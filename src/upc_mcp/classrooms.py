"""Independent anonymous read-only UPC free-classroom protocol.

This module deliberately has no dependency on the campus-card balance flow,
Shiguang, CAS, cookies, WeChat, the retired JHZF bill flow, or local credential
storage. It sends one fixed public form request and parses one public JSON
response.

Verified upstream facts (2026-10-27 anonymous page check):

* entry page: ``https://app.upc.edu.cn/freeclass/wap/default/all``
* query: ``POST https://app.upc.edu.cn/freeclass/wap/default/search-all``
* UTF-8 form: ``xq=<campus>&date=YYYY-MM-DD&lh=<building or 全部>`` plus
  repeated, ascending ``jc[]=1..13`` fields;
* ``Accept: application/json``; no account, Cookie, or token is required;
* success: ``e=0``; ``d.all`` is the complete room directory grouped by
  building, ``d.js`` is the union of rooms occupied in any selected period,
  and ``d.listLh`` is the building option list;
* compare rooms by ``code``.  A room is unavailable for a multi-period
  selection if it appears for any selected period.  The aggregate cannot
  reconstruct per-period state, so this module never fabricates it.

No redirect is followed.  No automatic retry, keep-alive loop, background
refresh, or school request at import/startup time is implemented.
"""

from __future__ import annotations

import json
import re
import ssl
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date as Date
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import httpx

ENDPOINT = "https://app.upc.edu.cn/freeclass/wap/default/search-all"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_CONCURRENCY = 4
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
    "晚上": (10, 11, 12, 13),
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
            "节次必须是 1–13 的整数数组，且至少选择一节。",
        )
    periods: set[int] = set()
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int) or not 1 <= item <= 13:
            raise ClassroomError(
                "invalid_periods",
                "节次必须是 1–13 的整数，且至少选择一节。",
            )
        periods.add(item)
    if not periods:
        raise ClassroomError(
            "invalid_periods",
            "节次必须是 1–13 的整数，且至少选择一节。",
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
    occupied: bool

    def public(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "building": self.building,
            "campus": self.campus,
            "capacity": self.capacity,
            "occupied": self.occupied,
        }


@dataclass(frozen=True)
class ClassroomDirectory:
    rooms: tuple[ClassroomRoom, ...]
    building_options: tuple[BuildingOption, ...]

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


def parse_response(body: bytes | str, query: ClassroomQuery) -> ClassroomDirectory:
    """Parse one response while preserving the strict public-page contract.

    An empty directory is returned as an empty directory, never as "all rooms
    are free".  The caller decides whether that is an upstream anomaly or a
    valid empty building filter.
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
    occupied_groups = _grouped_rooms(data.get("js"), "js")
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

    occupied_codes: set[str] = set()
    for _group_name, entries in occupied_groups:
        for item in entries:
            occupied_codes.add(_required_text(item, "code"))
    unknown = occupied_codes - codes
    if unknown:
        raise _invalid("学校返回的占用教室未出现在完整教室目录中。")

    rooms = tuple(
        ClassroomRoom(
            code=room_code,
            name=room_name,
            building=building,
            campus=campus,
            capacity=capacity,
            occupied=room_code in occupied_codes,
        )
        for room_code, room_name, building, campus, capacity in raw_rooms
    )
    return ClassroomDirectory(rooms=rooms, building_options=building_options)


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


class FreeClassroomClient:
    """HTTP adapter for the one fixed anonymous classroom endpoint."""

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: httpx.Timeout | None = None,
    ):
        self.transport = transport
        self.timeout = timeout or REQUEST_TIMEOUT

    async def fetch(self, query: ClassroomQuery) -> ClassroomDirectory:
        form = build_form(query)
        try:
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
                async with client.stream(
                    "POST",
                    ENDPOINT,
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
                        if (
                            declared_size is not None
                            and declared_size > MAX_RESPONSE_BYTES
                        ):
                            raise ClassroomError(
                                "response_too_large",
                                "学校教室响应超过本工具 2 MiB 上限。",
                            )

                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
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

        return parse_response(bytes(body), query)

