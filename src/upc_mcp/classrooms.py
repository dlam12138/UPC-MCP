"""Independent anonymous read-only UPC free-classroom protocol.

This module deliberately has no dependency on the campus-card balance flow,
Shiguang, CAS, cookies, WeChat, the retired JHZF bill flow, or local credential
storage. It sends fixed public form requests and parses public JSON responses.

Verified upstream facts (2026-11-02 anonymous page check):

* entry page: ``https://app.upc.edu.cn/freeclass/wap/default/all``
* catalog query: ``POST https://app.upc.edu.cn/freeclass/wap/default/search-all``
* UTF-8 form: ``xq=<campus>&date=YYYY-MM-DD&lh=<building or 全部>`` plus
  repeated, ascending ``jc[]=1..13`` fields;
* ``Accept: application/json``; no account, Cookie, or token is required;
* ``d.all`` is the complete room directory grouped by building, and
  ``d.listLh`` is the building option list.  Those two fields are the only
  inputs used from ``search-all``.
* ``d.js`` is *not* used for occupancy.  The observed page watches it and may
  mark rooms as used, but it contradicts per-room detail for sampled rooms
  (for example ZY-B116).  Missing, malformed or changed ``d.js`` is retained
  only as an ignored diagnostic and never blocks a valid directory.
* Per-room occupancy comes only from ``POST .../detail``.  ``d.weekdays``
  maps the query date to a ``d.kclist`` weekday key; every non-empty ``c_*``
  block on that day contributes its ``lessArr`` periods.  A non-empty
  intersection with the selected periods means occupied; an empty
  intersection means the detail record shows no occupancy.
* Detail failure, a missing date, a malformed structure or an unqueried room
  means ``unknown``, never free and never occupied-by-fallback.  If every
  planned detail request fails, the query returns ``availability_unverified``.
* Room names containing ``研究生专用``/``教师专用``/``专用教室`` are retained
  as name-only access hints.  They do not prove admission and are kept
  separate from period occupancy.

No redirect is followed.  No automatic retry, keep-alive loop, background
refresh, or school request at import/startup time is implemented.
"""

from __future__ import annotations

import asyncio
import json
import re
import ssl
from collections.abc import Iterable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import date as Date
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from urllib.parse import urlencode

import httpx

ENDPOINT = "https://app.upc.edu.cn/freeclass/wap/default/search-all"
DETAIL_ENDPOINT = "https://app.upc.edu.cn/freeclass/wap/default/detail"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_CONCURRENCY = 4
DETAIL_CONCURRENCY = 4
DETAIL_DEADLINE_SECONDS = 30.0
MAX_DETAIL_REQUESTS = 300
DETAIL_STOP_CODES = frozenset(
    {
        "rate_limited",
        "access_denied",
        "network_error",
        "tls_error",
        "timeout",
        "redirect",
    }
)
RESERVED_NAME_MARKERS = ("研究生专用", "教师专用", "专用教室")


def is_reserved_name(name: str) -> bool:
    return any(marker in name for marker in RESERVED_NAME_MARKERS)
CONNECT_TIMEOUT_SECONDS = 10.0
READ_TIMEOUT_SECONDS = 15.0
DEFAULT_ROOM_LIMIT = 200
MAX_ROOM_LIMIT = 500
MAX_ROOM_OFFSET = 100_000
REQUEST_TIMEOUT = httpx.Timeout(
    connect=CONNECT_TIMEOUT_SECONDS,
    read=READ_TIMEOUT_SECONDS,
    write=10.0,
    pool=10.0,
)
REQUEST_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
}
CHINA_TIMEZONE = timezone(timedelta(hours=8), "Asia/Shanghai")

CAMPUSES = ("唐岛湾校区", "古镇口校区")
CAMPUS_ALIASES = {
    "唐岛湾校区": "唐岛湾校区",
    "唐岛湾": "唐岛湾校区",
    "古镇口校区": "古镇口校区",
    "古镇口": "古镇口校区",
}
PERIOD_GROUPS = {
    "上午": (1, 2, 3, 4, 5),
    "下午": (6, 7, 8, 9),
    "晚上": (10, 11, 12),
}
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
INTEGER_RE = re.compile(r"-?[0-9]+\Z")
MIN_DATE = Date(2000, 1, 1)
MAX_DATE = Date(2100, 12, 31)


class ClassroomError(Exception):
    """Fixed, credential-free error crossing the adapter/service boundary."""

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None):
        self.code = code
        self.details = details or {}
        super().__init__(message)

    def payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.code,
            "source_system": "classrooms",
            "error": {"message": str(self)},
        }
        payload.update(self.details)
        return payload


def now() -> str:
    """Return a China-time timestamp without depending on a system tz database."""
    return datetime.now(CHINA_TIMEZONE).isoformat(timespec="seconds")


def _invalid(message: str) -> ClassroomError:
    return ClassroomError("schema_changed", message)


def _normalize_campus(value: object) -> str:
    if not isinstance(value, str):
        raise ClassroomError(
            "invalid_campus",
            "校区必须是唐岛湾校区或古镇口校区。",
            details={"allowed_campuses": list(CAMPUSES)},
        )
    campus = CAMPUS_ALIASES.get(value.strip())
    if campus is None:
        raise ClassroomError(
            "invalid_campus",
            "校区必须是唐岛湾校区或古镇口校区。",
            details={"allowed_campuses": list(CAMPUSES)},
        )
    return campus


def _normalize_date(value: object) -> Date:
    if not isinstance(value, str):
        raise ClassroomError("invalid_date", "日期必须显式提供为 YYYY-MM-DD。")
    raw = value.strip()
    if DATE_RE.fullmatch(raw) is None:
        raise ClassroomError("invalid_date", "日期必须显式提供为 YYYY-MM-DD。")
    try:
        parsed = Date.fromisoformat(raw)
    except ValueError:
        raise ClassroomError("invalid_date", "日期不是有效的公历日期。") from None
    if not MIN_DATE <= parsed <= MAX_DATE:
        raise ClassroomError(
            "invalid_date",
            f"日期必须位于 {MIN_DATE.isoformat()} 至 {MAX_DATE.isoformat()} 之间。",
        )
    return parsed


def _normalize_periods(value: object) -> tuple[int, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(
        value, (list, tuple, set, frozenset)
    ):
        raise ClassroomError(
            "invalid_periods",
            "节次必须是 1–12 的整数数组，且至少选择一节；第 13 节尚未验证。",
        )
    periods: set[int] = set()
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            raise ClassroomError(
                "invalid_periods",
                "节次必须是 1–12 的整数，且至少选择一节；第 13 节尚未验证。",
            )
        if item == 13:
            raise ClassroomError(
                "unsupported_period",
                "第 13 节尚未验证：search-all 返回全占用，detail 没有 c_13 数据；"
                "本工具不把它当作可用，也不猜成 1–12。",
                details={
                    "period": 13,
                    "supported_periods": [1, 12],
                    "reason": "detail_c13_empty",
                },
            )
        if not 1 <= item <= 12:
            raise ClassroomError(
                "invalid_periods",
                "节次必须是 1–12 的整数，且至少选择一节；第 13 节尚未验证。",
            )
        periods.add(item)
    if not periods:
        raise ClassroomError(
            "invalid_periods",
            "节次必须是 1–12 的整数，且至少选择一节；第 13 节尚未验证。",
        )
    return tuple(sorted(periods))


def _normalize_building(value: object) -> str:
    if not isinstance(value, str):
        raise ClassroomError("invalid_building", "教学楼必须是“全部”或学校返回的楼标识。")
    building = value.strip()
    if (
        not building
        or len(building) > 100
        or any(ord(character) < 32 or ord(character) == 127 for character in building)
    ):
        raise ClassroomError("invalid_building", "教学楼必须是“全部”或学校返回的楼标识。")
    return building


def selected_period_groups(periods: Iterable[int]) -> list[str]:
    selected = set(periods)
    return [
        label
        for label, group_periods in PERIOD_GROUPS.items()
        if any(period in selected for period in group_periods)
    ]


@dataclass(frozen=True)
class ClassroomQuery:
    campus: str
    date: Date
    building: str
    periods: tuple[int, ...]

    @classmethod
    def create(
        cls,
        *,
        campus: object,
        date: object,
        periods: object,
        building: object = "全部",
    ) -> ClassroomQuery:
        return cls(
            campus=_normalize_campus(campus),
            date=_normalize_date(date),
            building=_normalize_building(building),
            periods=_normalize_periods(periods),
        )

    def public(self) -> dict[str, Any]:
        return {
            "campus": self.campus,
            "date": self.date.isoformat(),
            "building": self.building,
            "periods": list(self.periods),
            "selected_period_groups": selected_period_groups(self.periods),
        }


@dataclass(frozen=True)
class BuildingOption:
    id: str
    name: str

    def public(self) -> dict[str, str]:
        return {"id": self.id, "name": self.name}


@dataclass(frozen=True)
class ClassroomRoom:
    code: str
    name: str
    building: str
    campus: str
    capacity: int | None
    occupancy: Literal["available", "occupied", "unknown"] = "unknown"
    occupancy_source: Literal["detail", "unknown", "unverified"] = "unverified"

    @property
    def access_restriction(self) -> tuple[str, ...]:
        """Name-only access hints; never proof of admission or exclusion."""

        return tuple(marker for marker in RESERVED_NAME_MARKERS if marker in self.name)

    @property
    def reserved(self) -> bool:
        """Deprecated compatibility alias for a name-only access hint."""

        return bool(self.access_restriction)

    @property
    def occupancy_unknown(self) -> bool:
        return self.occupancy == "unknown"

    @property
    def occupied(self) -> bool | None:
        """Compatibility projection of :attr:`occupancy`."""

        if self.occupancy == "unknown":
            return None
        return self.occupancy == "occupied"

    @property
    def ordinary_self_study(self) -> bool:
        return self.occupancy == "available" and not self.access_restriction

    @property
    def availability(self) -> str:
        if self.occupancy != "available":
            return self.occupancy
        return "available_restricted" if self.access_restriction else "available"

    def public(self) -> dict[str, Any]:
        markers = self.access_restriction
        return {
            "code": self.code,
            "name": self.name,
            "building": self.building,
            "campus": self.campus,
            "capacity": self.capacity,
            "occupied": None if self.occupancy == "unknown" else self.occupancy == "occupied",
            "occupancy": self.occupancy,
            "availability": self.availability,
            "occupancy_source": self.occupancy_source,
            "occupancy_unknown": self.occupancy_unknown,
            "access_restriction": (
                {
                    "kind": "name_marker",
                    "markers": list(markers),
                    "basis": "room_name",
                    "proves_access": False,
                }
                if markers
                else None
            ),
            "reserved": bool(markers),
            "ordinary_self_study": self.ordinary_self_study,
            "search_all_conflict": False,
        }


@dataclass(frozen=True)
class RoomDetail:
    """One room's per-period occupancy from the official detail endpoint."""

    code: str
    date: Date
    occupied_periods: frozenset[int]


@dataclass(frozen=True)
class DetailLookup:
    """A detail lookup result; ``None`` periods mean the room is unverified."""

    code: str
    occupied_periods: frozenset[int] | None
    error: str | None = None
    http_status: int | None = None

    @property
    def verified(self) -> bool:
        return self.occupied_periods is not None


@dataclass(frozen=True)
class ClassroomVerification:
    """Coverage of per-room detail records.

    ``search_all_occupied`` is retained only as an ignored diagnostic count of
    the observed ``d.js`` payload; it does not participate in any decision.
    ``availability_certainty`` therefore depends only on detail coverage and
    detail evidence, not on access-restriction name hints.
    """

    search_all_occupied: int = 0
    detail_checked: int = 0
    detail_occupied: int = 0
    detail_failed: int = 0
    unknown: int = 0
    restricted: int = 0
    unqueried: int = 0
    timed_out: int = 0
    checked_all_rooms: bool = True

    @property
    def request_complete(self) -> bool:
        """Whether every planned detail request completed without failure."""

        return self.detail_failed == 0 and self.unqueried == 0

    @property
    def conflicts(self) -> int:
        """Deprecated compatibility constant; search-all conflicts are gone."""

        return 0

    @property
    def reserved(self) -> int:
        """Deprecated compatibility alias for ``restricted``."""

        return self.restricted

    @property
    def complete(self) -> bool:
        """Deprecated alias for :attr:`request_complete`."""

        return self.request_complete

    @property
    def availability_certainty(self) -> str:
        if not self.request_complete:
            return "partial"
        if not self.checked_all_rooms:
            return "partial"
        if self.unknown:
            return "partial"
        return "records_verified"

    def public(self) -> dict[str, Any]:
        return {
            "search_all_occupied": self.search_all_occupied,
            "search_all_occupied_used": False,
            "detail_checked": self.detail_checked,
            "detail_occupied": self.detail_occupied,
            "detail_failed": self.detail_failed,
            "unknown": self.unknown,
            "conflicts": 0,
            "reserved": self.restricted,
            "restricted": self.restricted,
            "unqueried": self.unqueried,
            "timed_out": self.timed_out,
            "checked_all_rooms": self.checked_all_rooms,
            "request_complete": self.request_complete,
            "availability_certainty": self.availability_certainty,
            "complete": self.complete,
        }


@dataclass(frozen=True)
class ClassroomDirectory:
    rooms: tuple[ClassroomRoom, ...]
    building_options: tuple[BuildingOption, ...]
    search_all_diagnostic: dict[str, Any] = dataclass_field(default_factory=dict)

    def public_buildings(self) -> list[str]:
        return [option.id for option in self.building_options]

    def public_building_options(self) -> list[dict[str, str]]:
        return [option.public() for option in self.building_options]


def build_form(query: ClassroomQuery) -> str:
    """Build the exact observed form body in stable field order."""
    fields: list[tuple[str, str]] = [
        ("xq", query.campus),
        ("date", query.date.isoformat()),
        ("lh", query.building),
    ]
    fields.extend(("jc[]", str(period)) for period in query.periods)
    return urlencode(fields, encoding="utf-8", errors="strict")


def build_detail_form(code: str, day: Date) -> str:
    """Build the official per-room detail form for one date."""
    return urlencode(
        [("code", code), ("date", day.isoformat())], encoding="utf-8", errors="strict"
    )


def _reject_constant(_value: str) -> None:
    raise ValueError


def _load_json(body: bytes | str) -> object:
    try:
        if isinstance(body, bytes):
            text = body.decode("utf-8-sig")
        elif isinstance(body, str):
            text = body.lstrip("\ufeff")
        else:
            raise TypeError
        return json.loads(text, parse_constant=_reject_constant)
    except (UnicodeDecodeError, TypeError, ValueError):
        raise _invalid("学校返回的教室响应不是有效 JSON。") from None


def _required_text(item: dict[str, Any], key: str) -> str:
    value = item.get(key)
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise _invalid(f"学校返回的教室字段 {key} 类型已变化。")
    text = str(value).strip()
    if not text:
        raise _invalid(f"学校返回的教室字段 {key} 为空。")
    return text


def _optional_capacity(item: dict[str, Any]) -> int | None:
    if "num" not in item or item["num"] is None:
        return None
    value = item["num"]
    # The Kotlin source treats an unparseable/missing capacity as unknown, not
    # as a protocol failure. A non-positive value is also unknown.
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    text = str(value).strip()
    if not text or INTEGER_RE.fullmatch(text) is None:
        return None
    capacity = int(text)
    return capacity if capacity > 0 else None


def _grouped_rooms(value: object, field_name: str) -> list[tuple[str, list[dict[str, Any]]]]:
    if isinstance(value, dict):
        groups: list[tuple[str, list[dict[str, Any]]]] = []
        for group_name, entries in value.items():
            if not isinstance(group_name, str) or not group_name.strip():
                raise _invalid(f"学校返回的 {field_name} 楼宇分组名无效。")
            if not isinstance(entries, list):
                raise _invalid(f"学校返回的 {field_name} 楼宇分组结构已变化。")
            group_rooms: list[dict[str, Any]] = []
            for entry in entries:
                if not isinstance(entry, dict):
                    raise _invalid(f"学校返回的 {field_name} 教室记录结构已变化。")
                group_rooms.append(entry)
            groups.append((group_name.strip(), group_rooms))
        return groups
    # PHP may encode an empty associative array as [].
    if isinstance(value, list):
        if not value:
            return []
        raise _invalid(f"学校返回的 {field_name} 非空数组无法按楼宇解析。")
    raise _invalid(f"学校返回的 {field_name} 字段缺失或类型已变化。")


def _parse_building_options(value: object) -> tuple[BuildingOption, ...]:
    if not isinstance(value, list):
        raise _invalid("学校返回的教学楼选项结构已变化。")
    options: list[BuildingOption] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise _invalid("学校返回的教学楼选项记录结构已变化。")
        option_id = _required_text(item, "id")
        if option_id in seen:
            continue
        raw_name = item.get("name", option_id)
        if raw_name is None:
            name = option_id
        elif isinstance(raw_name, bool) or not isinstance(raw_name, (str, int)):
            raise _invalid("学校返回的教学楼名称类型已变化。")
        else:
            name = str(raw_name).strip() or option_id
        options.append(BuildingOption(id=option_id, name=name))
        seen.add(option_id)
    if "全部" not in seen:
        options.insert(0, BuildingOption(id="全部", name="全部"))
    return tuple(options)


def _integer_field(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise _invalid(f"学校返回的 {field_name} 字段类型已变化。")
    text = str(value).strip()
    if INTEGER_RE.fullmatch(text) is None:
        raise _invalid(f"学校返回的 {field_name} 字段不是整数。")
    return int(text)


def _optional_message(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
    elif isinstance(value, int) and not isinstance(value, bool):
        text = str(value)
    else:
        return None
    return text[:300] if text else None


def _diagnose_search_all_js(
    value: object, directory_codes: set[str]
) -> dict[str, Any]:
    """Inspect ``d.js`` without letting it affect any occupancy decision."""

    diagnostic: dict[str, Any] = {
        "present": value is not None,
        "used_for_occupancy": False,
        "parse_error": None,
        "occupied_code_count": None,
        "unknown_code_count": None,
    }
    if value is None:
        return diagnostic
    try:
        groups = _grouped_rooms(value, "js")
        codes: set[str] = set()
        for _group_name, entries in groups:
            for item in entries:
                codes.add(_required_text(item, "code"))
    except ClassroomError:
        # The page may change or omit this field.  A malformed d.js is data
        # noise, not a reason to reject a valid d.all directory.
        diagnostic["parse_error"] = "invalid_js"
        return diagnostic
    diagnostic["occupied_code_count"] = len(codes)
    diagnostic["unknown_code_count"] = len(codes - directory_codes)
    return diagnostic


def parse_response(body: bytes | str, query: ClassroomQuery) -> ClassroomDirectory:
    """Parse the directory from one ``search-all`` response.

    Only ``d.all`` and ``d.listLh`` are required.  Room occupancy is not read
    from ``d.js``: every room starts as ``unknown`` until the per-room detail
    record is parsed.  An empty directory is returned as an empty directory,
    never as "all rooms are free".
    """

    root = _load_json(body)
    if not isinstance(root, dict):
        raise _invalid("学校返回的教室响应结构已变化。")
    if "e" not in root:
        raise _invalid("学校返回的教室响应缺少业务状态。")
    code = _integer_field(root["e"], "e")
    if code != 0:
        message = _optional_message(root.get("m")) or "学校教室查询返回业务错误。"
        raise ClassroomError(
            "business_error",
            "学校教室查询未返回有效结果，请稍后重试。",
            details={"upstream_code": code, "upstream_message": message},
        )
    data = root.get("d")
    if not isinstance(data, dict):
        raise _invalid("学校返回的教室响应缺少数据对象。")

    all_groups = _grouped_rooms(data.get("all"), "all")
    building_options = _parse_building_options(data.get("listLh"))

    raw_rooms: list[tuple[str, str, str, str, int | None]] = []
    codes: set[str] = set()
    for group_name, entries in all_groups:
        for item in entries:
            room_code = _required_text(item, "code")
            room_name = _required_text(item, "name")
            campus = _required_text(item, "xqm")
            building = _required_text(item, "jxlm")
            if building != group_name:
                raise _invalid("学校返回的教室楼宇归属与分组不一致。")
            if campus != query.campus:
                raise _invalid("学校返回了与查询校区不一致的教室。")
            if room_code in codes:
                raise _invalid("学校返回的教室目录含重复 code。")
            codes.add(room_code)
            raw_rooms.append(
                (room_code, room_name, building, campus, _optional_capacity(item))
            )

    rooms = tuple(
        ClassroomRoom(
            code=room_code,
            name=room_name,
            building=building,
            campus=campus,
            capacity=capacity,
        )
        for room_code, room_name, building, campus, capacity in raw_rooms
    )
    return ClassroomDirectory(
        rooms=rooms,
        building_options=building_options,
        search_all_diagnostic=_diagnose_search_all_js(data.get("js"), codes),
    )


def parse_detail(body: bytes | str, query: ClassroomQuery, code: str) -> RoomDetail:
    """Parse one official room-detail response into the periods it occupies.

    The page's ``kclist`` is keyed by weekday (1–7) in the same order as
    ``weekdays``.  A multi-period block is stored only on its starting period
    (for example ``c_10.lessArr == [10, 11, 12]``; ``c_11``/``c_12`` are
    empty), so callers must inspect ``lessArr`` rather than ``c_<period>``.
    """
    root = _load_json(body)
    if not isinstance(root, dict):
        raise _invalid("学校返回的教室详情结构已变化。")
    if "e" not in root:
        raise _invalid("学校返回的教室详情缺少业务状态。")
    status = _integer_field(root["e"], "e")
    if status != 0:
        message = _optional_message(root.get("m")) or "学校教室详情返回业务错误。"
        raise ClassroomError(
            "business_error",
            "学校教室详情未返回有效结果，请稍后重试。",
            details={"upstream_code": status, "upstream_message": message},
        )
    data = root.get("d")
    if not isinstance(data, dict):
        raise _invalid("学校返回的教室详情缺少数据对象。")
    weekdays = data.get("weekdays")
    if not isinstance(weekdays, list) or not weekdays:
        raise _invalid("学校返回的教室详情缺少周日期。")
    target = query.date.isoformat()
    index = None
    for position, value in enumerate(weekdays):
        if not isinstance(value, str):
            raise _invalid("学校返回的教室详情周日期格式已变化。")
        if value == target:
            index = position
            break
    if index is None:
        raise ClassroomError(
            "detail_date_missing",
            "学校教室详情未包含查询日期；未采信该房间为空闲。",
        )
    kclist = data.get("kclist")
    if not isinstance(kclist, dict):
        raise _invalid("学校返回的教室详情缺少课表。")
    day = kclist.get(str(index + 1))
    if not isinstance(day, dict):
        raise _invalid("学校返回的教室详情缺少对应星期的课表。")
    periods: set[int] = set()
    saw_period_key = False
    for key, block in day.items():
        if (
            not isinstance(key, str)
            or not key.startswith("c_")
            or not key[2:].isdigit()
        ):
            if block not in (None, [], {}):
                raise _invalid("学校返回的教室详情课块键已变化。")
            continue
        saw_period_key = True
        number = int(key[2:])
        if not 1 <= number <= 13:
            raise _invalid("学校返回的教室详情课块节次超出 1–13。")
        if block is None or block == [] or block == {}:
            continue
        if not isinstance(block, dict):
            raise _invalid("学校返回的教室详情课块结构已变化。")
        less = block.get("lessArr")
        if not isinstance(less, list) or not less:
            raise _invalid("学校返回的教室详情课块缺少 lessArr。")
        for period in less:
            if isinstance(period, bool) or not isinstance(period, int):
                raise _invalid("学校返回的教室详情节次类型已变化。")
            if not 1 <= period <= 13:
                raise _invalid("学校返回的教室详情节次超出 1–13。")
            periods.add(period)
    if not saw_period_key:
        raise _invalid("学校返回的教室详情缺少 c_* 课块字段。")
    return RoomDetail(code=code, date=query.date, occupied_periods=frozenset(periods))


def _is_tls_error(error: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ssl.SSLError):
            return True
        message = str(current).lower()
        if "certificate verify failed" in message or "ssl handshake" in message:
            return True
        current = current.__cause__ or current.__context__
    return False


@asynccontextmanager
async def _optional_gate(gate: asyncio.Semaphore | None):
    if gate is None:
        yield
    else:
        async with gate:
            yield


class FreeClassroomClient:
    """HTTP adapter for the two fixed anonymous classroom endpoints."""

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: httpx.Timeout | None = None,
    ):
        self.transport = transport
        self.timeout = timeout or REQUEST_TIMEOUT

    async def _post(
        self,
        client: httpx.AsyncClient,
        url: str,
        form: str,
        *,
        limit: int = MAX_RESPONSE_BYTES,
    ) -> bytes:
        try:
            async with client.stream(
                "POST",
                url,
                content=form.encode("utf-8"),
                headers=REQUEST_HEADERS,
            ) as response:
                status = response.status_code
                if 300 <= status < 400:
                    raise ClassroomError(
                        "redirect",
                        "学校教室服务要求跳转，当前不跟随重定向。",
                        details={"http_status": status},
                    )
                if status in (401, 403):
                    raise ClassroomError(
                        "access_denied",
                        "学校教室服务拒绝了匿名请求，未继续重试。",
                        details={"http_status": status},
                    )
                if status == 429:
                    raise ClassroomError(
                        "rate_limited",
                        "学校教室服务暂时限流，未继续重试。",
                        details={"http_status": status},
                    )
                if status != 200:
                    raise ClassroomError(
                        "upstream_error",
                        f"学校教室服务返回 HTTP {status}。",
                        details={"http_status": status},
                    )

                declared = response.headers.get("content-length")
                if declared is not None:
                    try:
                        declared_size = int(declared)
                    except ValueError:
                        declared_size = None
                    if declared_size is not None and declared_size > limit:
                        raise ClassroomError(
                            "response_too_large",
                            "学校教室响应超过本工具 2 MiB 上限。",
                        )

                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > limit:
                        raise ClassroomError(
                            "response_too_large",
                            "学校教室响应超过本工具 2 MiB 上限。",
                        )
                    body.extend(chunk)
        except ClassroomError:
            raise
        except httpx.TimeoutException:
            raise ClassroomError(
                "timeout",
                "学校教室查询超时，未自动重试。",
            ) from None
        except (httpx.HTTPError, OSError) as error:
            if _is_tls_error(error):
                raise ClassroomError(
                    "tls_error",
                    "学校教室服务的 TLS 校验失败；未降级证书校验。",
                ) from None
            raise ClassroomError(
                "network_error",
                "无法连接学校教室服务，请检查网络后重试。",
            ) from None
        return bytes(body)

    async def fetch(
        self,
        query: ClassroomQuery,
        *,
        gate: asyncio.Semaphore | None = None,
    ) -> ClassroomDirectory:
        form = build_form(query)
        async with httpx.AsyncClient(
            transport=self.transport,
            trust_env=False,
            follow_redirects=False,
            timeout=self.timeout,
            limits=httpx.Limits(
                max_connections=MAX_CONCURRENCY,
                max_keepalive_connections=0,
            ),
        ) as client:
            async with _optional_gate(gate):
                body = await self._post(client, ENDPOINT, form)
        return parse_response(body, query)

    async def fetch_details(
        self,
        query: ClassroomQuery,
        codes: Sequence[str],
        *,
        concurrency: int = DETAIL_CONCURRENCY,
        deadline: float = DETAIL_DEADLINE_SECONDS,
        gate: asyncio.Semaphore | None = None,
    ) -> dict[str, DetailLookup]:
        """Verify every room in the selected scope through official detail.

        Individual failures are returned as unknown lookups; they must never be
        interpreted as free or as occupied by a search-all fallback.  Every
        request is issued by this one client and is bounded by ``concurrency``
        and the overall ``deadline``.
        """
        unique = list(dict.fromkeys(codes))
        if not unique:
            return {}
        if len(unique) > MAX_DETAIL_REQUESTS:
            raise ClassroomError(
                "too_many_details",
                "所选范围教室数量超过本地逐间校验上限；未返回未校验的结论。",
                details={"rooms": len(unique), "limit": MAX_DETAIL_REQUESTS},
            )
        if (
            isinstance(concurrency, bool)
            or not isinstance(concurrency, int)
            or not 1 <= concurrency <= 8
        ):
            concurrency = DETAIL_CONCURRENCY
        if (
            isinstance(deadline, bool)
            or not isinstance(deadline, (int, float))
            or not deadline > 0
        ):
            deadline = DETAIL_DEADLINE_SECONDS

        stop = asyncio.Event()
        semaphore = asyncio.Semaphore(concurrency)
        results: dict[str, DetailLookup] = {}

        async def one(code: str) -> DetailLookup:
            if stop.is_set():
                return DetailLookup(code, None, "detail_skipped")
            async with semaphore:
                if stop.is_set():
                    return DetailLookup(code, None, "detail_skipped")
                try:
                    async with _optional_gate(gate):
                        if stop.is_set():
                            return DetailLookup(code, None, "detail_skipped")
                        body = await self._post(
                            client,
                            DETAIL_ENDPOINT,
                            build_detail_form(code, query.date),
                        )
                    detail = parse_detail(body, query, code)
                    return DetailLookup(code, detail.occupied_periods)
                except ClassroomError as error:
                    if error.code in DETAIL_STOP_CODES:
                        stop.set()
                    status = error.details.get("http_status")
                    return DetailLookup(
                        code,
                        None,
                        error.code,
                        status if isinstance(status, int) else None,
                    )
                except Exception:
                    return DetailLookup(code, None, "detail_error")

        async with httpx.AsyncClient(
            transport=self.transport,
            trust_env=False,
            follow_redirects=False,
            timeout=self.timeout,
            limits=httpx.Limits(
                max_connections=concurrency,
                max_keepalive_connections=concurrency,
            ),
        ) as client:
            tasks = {asyncio.create_task(one(code)): code for code in unique}
            try:
                done, pending = await asyncio.wait(tasks, timeout=deadline)
                for task in done:
                    try:
                        lookup = task.result()
                    except Exception:
                        lookup = DetailLookup(tasks[task], None, "detail_error")
                    results[lookup.code] = lookup
                for task in pending:
                    code = tasks[task]
                    task.cancel()
                    results[code] = DetailLookup(code, None, "detail_timeout")
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
            finally:
                # Cancellation of the parent must not leave HTTP tasks running
                # or let a later query inherit them.  Cancel and drain every
                # child before the owning AsyncClient closes.
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        for code in unique:
            results.setdefault(code, DetailLookup(code, None, "detail_error"))
        return results

