"""Anonymous read-only Tangdaowan remaining-electricity protocol.

The verified upstream chain (2026-10-28 device check, re-checked as a public
form GET on 2026-11-02) is:

* ``GET http://dfcz.upc.edu.cn/ShiYou/Default.aspx?code=0`` returns an ASP.NET
  WebForms page with ``form1``, three hidden fields and a ``lou`` building
  option list.  The fixed ``code=0`` value is not an OAuth identity; without a
  ``code`` parameter the page redirected to WeChat OAuth in the observed
  period.
* ``POST http://dfcz.upc.edu.cn/ShiYou/Default.aspx`` submits
  ``__VIEWSTATE``, ``__VIEWSTATEGENERATOR``, ``__EVENTVALIDATION``, ``lou``,
  ``yonghu`` and ``chaxun=查询``.
* A unique room result is rendered in ``yongdianGrid`` with the exact label
  ``当前剩余电量`` and a value such as ``17.34度``.  The hidden columns carry
  the school system number and room number.
* The school may fuzzy-match a room.  A candidate table
  ``yonghuGridView`` (system number / building / room) can be returned together
  with the first room's reading.  This module therefore requires a unique exact
  building + room match; it never adopts the first candidate or a reading that
  belongs to another room.

The unit is kWh (``度``).  It is a remaining-electricity reading, not a money
balance, not an amount owed, and not an arrears field.  The page provides no
meter update time; ``fetched_at`` is the local query time.  No account, CAS
ticket, Cookie, URL, header or saved room target is accepted.  One query issues
exactly one GET and one POST, with no redirect following, proxy use, retry,
keep-alive loop, cache or persistent state.

The school endpoint is HTTP.  There is no TLS to verify for this observed
endpoint; this module does not downgrade or disable certificate verification
for other systems.
"""

from __future__ import annotations

import html as html_lib
import re
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlencode

import httpx

SOURCE_SYSTEM = "electricity"
CAMPUS = "唐岛湾"
CAMPUS_ID = "tangdaowan"
SCHEME = "http"
HOST = "dfcz.upc.edu.cn"
PAGE_PATH = "/ShiYou/Default.aspx"
NEUTRAL_CODE = "0"
GET_URL = f"{SCHEME}://{HOST}{PAGE_PATH}?code={NEUTRAL_CODE}"
POST_URL = f"{SCHEME}://{HOST}{PAGE_PATH}"
MAX_RESPONSE_BYTES = 1024 * 1024
CONNECT_TIMEOUT_SECONDS = 10.0
READ_TIMEOUT_SECONDS = 20.0
MAX_BUILDING_LENGTH = 100
MAX_ROOM_LENGTH = 64
MAX_CONCURRENCY = 1
REQUEST_TIMEOUT = httpx.Timeout(
    connect=CONNECT_TIMEOUT_SECONDS,
    read=READ_TIMEOUT_SECONDS,
    write=10.0,
    pool=10.0,
)
REQUEST_HEADERS = {
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Cache-Control": "no-cache, no-store",
}
FORM_HEADERS = {
    **REQUEST_HEADERS,
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
}
CHINA_TIMEZONE = timezone(timedelta(hours=8), "Asia/Shanghai")
KWH_RE = re.compile(r"(?P<number>[0-9]{1,18}(?:\.[0-9]{1,12})?)\s*度\Z")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
TABLE_ID_RE = re.compile(r"""id\s*=\s*["'](?P<id>yongdianGrid|yonghuGridView)["']""", re.I)


class ElectricityError(Exception):
    """Fixed, credential-free error crossing the client/service boundary."""

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None):
        self.code = code
        self.details = details or {}
        super().__init__(message)

    def payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.code,
            "source_system": SOURCE_SYSTEM,
            "campus": CAMPUS,
            "error": {"message": str(self)},
        }
        payload.update(self.details)
        return payload


def now() -> str:
    """Return a China-time timestamp without depending on the system tz database."""
    return datetime.now(CHINA_TIMEZONE).isoformat(timespec="seconds")


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", html_lib.unescape(value).replace("\u00a0", " ")).strip()


def _has_control(value: str) -> bool:
    return CONTROL_RE.search(value) is not None


def _invalid(message: str) -> ElectricityError:
    return ElectricityError("schema_changed", message)


def _missing(message: str = "必须同时提供本次用户明确给出的楼号和房间号。") -> ElectricityError:
    return ElectricityError(
        "missing_input",
        message,
        details={"campus": CAMPUS, "missing": ["building", "room"], "persistence": "none"},
    )


def validate_input(building: object, room: object) -> tuple[str, str]:
    """Validate the two explicit per-call query parameters.

    Both are mandatory.  This function never supplies a default, reads a saved
    target, or derives a room from an account.  A blank value is
    ``missing_input``; an overlong or control-character value is
    ``invalid_input``.
    """
    if building is None or (isinstance(building, str) and not building.strip()):
        raise _missing()
    if room is None or (isinstance(room, str) and not room.strip()):
        raise _missing()
    missing: list[str] = []
    if not isinstance(building, str):
        raise ElectricityError(
            "invalid_input",
            "building 必须是字符串。",
            details={"campus": CAMPUS, "field": "building"},
        )
    if not isinstance(room, str):
        raise ElectricityError(
            "invalid_input",
            "room 必须是字符串。",
            details={"campus": CAMPUS, "field": "room"},
        )
    if _has_control(building):
        raise ElectricityError(
            "invalid_input",
            "building 不能包含控制字符。",
            details={"campus": CAMPUS, "field": "building"},
        )
    if _has_control(room):
        raise ElectricityError(
            "invalid_input",
            "room 不能包含控制字符。",
            details={"campus": CAMPUS, "field": "room"},
        )
    building_value = _normalize_text(building)
    room_value = _normalize_text(room)
    if not building_value:
        missing.append("building")
    if not room_value:
        missing.append("room")
    if missing:
        raise ElectricityError(
            "missing_input",
            "必须同时提供本次用户明确给出的楼号和房间号；未从账号、历史或默认宿舍猜测。",
            details={"campus": CAMPUS, "missing": missing, "persistence": "none"},
        )
    if len(building_value) > MAX_BUILDING_LENGTH or _has_control(building_value):
        raise ElectricityError(
            "invalid_input",
            f"building 必须是 1–{MAX_BUILDING_LENGTH} 个可见字符。",
            details={"campus": CAMPUS, "field": "building"},
        )
    if len(room_value) > MAX_ROOM_LENGTH or _has_control(room_value):
        raise ElectricityError(
            "invalid_input",
            f"room 必须是 1–{MAX_ROOM_LENGTH} 个可见字符。",
            details={"campus": CAMPUS, "field": "room"},
        )
    return building_value, room_value


@dataclass(frozen=True)
class BuildingOption:
    id: str
    name: str

    def public(self) -> dict[str, str]:
        return {"id": self.id, "name": self.name}


@dataclass(frozen=True)
class PowerForm:
    view_state: str
    view_state_generator: str
    event_validation: str
    buildings: tuple[BuildingOption, ...]


@dataclass(frozen=True)
class Candidate:
    system_no: str
    building: str
    room: str

    def public(self) -> dict[str, str]:
        return {
            "system_no": self.system_no,
            "building": self.building,
            "room": self.room,
        }


@dataclass(frozen=True)
class Reading:
    building: str
    room: str
    system_no: str
    kwh: Decimal

    def public(self) -> dict[str, Any]:
        return {
            "reading_kind": "remaining_electricity",
            "source_field": "当前剩余电量",
            "remaining_kwh": format_kwh(self.kwh),
            "unit": "kWh",
            "unit_label": "度",
            "is_money_balance": False,
            "money_balance": None,
            "money_balance_status": "not_provided_by_source",
            "arrears": None,
            "arrears_status": "not_provided_by_source",
            "building": self.building,
            "room": self.room,
            "system_no": self.system_no or None,
            "zero_reading": self.kwh == 0,
            "upstream_updated_at": None,
            "upstream_updated_at_status": "not_provided_by_source",
        }


@dataclass(frozen=True)
class Ambiguous:
    candidates: tuple[Candidate, ...]
    reason: str

    def public(self) -> dict[str, Any]:
        return {
            "reading_available": False,
            "candidate_count": len(self.candidates),
            "candidates": [candidate.public() for candidate in self.candidates],
            "reason": self.reason,
        }


@dataclass
class _TableCapture:
    id: str
    depth: int = 1
    rows: list[list[str]] = field(default_factory=list)


@dataclass
class _Option:
    value: str
    text: str
    selected: bool


class _PageParser(HTMLParser):
    HIDDEN_FIELDS = frozenset(
        {"__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION"}
    )
    TABLE_IDS = frozenset({"yongdianGrid", "yonghuGridView"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.wechat_redirect = False
        self.has_form1 = False
        self.hidden: dict[str, str] = {}
        self.options: list[_Option] = []
        self.tables: dict[str, list[list[str]]] = {}
        self.duplicate_tables: set[str] = set()
        self._lou_select_depth = 0
        self._option: _Option | None = None
        self._option_text: list[str] = []
        self._table: _TableCapture | None = None
        self._row: list[str] = []
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {
            key.lower(): (value if value is not None else "") for key, value in attrs
        }
        if any("open.weixin.qq.com" in value.lower() for value in attributes.values()):
            self.wechat_redirect = True
        if tag == "form" and attributes.get("id", "").strip() == "form1":
            self.has_form1 = True
        elif tag == "input":
            if attributes.get("type", "").strip().lower() == "hidden":
                name = attributes.get("name", "").strip()
                if name in self.HIDDEN_FIELDS and name not in self.hidden:
                    self.hidden[name] = html_lib.unescape(attributes.get("value", ""))
        elif tag == "select" and attributes.get("name", "").strip() == "lou":
            self._lou_select_depth += 1
        elif tag == "option" and self._lou_select_depth:
            self._option = _Option(
                value=html_lib.unescape(attributes.get("value", "")),
                text="",
                selected="selected" in attributes,
            )
            self._option_text = []
        elif tag == "table":
            if self._table is None:
                table_id = attributes.get("id", "").strip()
                if table_id in self.TABLE_IDS:
                    self._table = _TableCapture(id=table_id)
            else:
                self._table.depth += 1
        elif tag == "tr" and self._table is not None and self._table.depth == 1:
            self._row = []
        elif tag == "td" and self._table is not None and self._table.depth == 1:
            self._cell = []

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if "open.weixin.qq.com" in data.lower():
            self.wechat_redirect = True
        if self._option is not None:
            self._option_text.append(data)
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "select" and self._lou_select_depth:
            self._lou_select_depth -= 1
        elif tag == "option" and self._option is not None:
            text = _normalize_text("".join(self._option_text))
            value = _normalize_text(self._option.value) or text
            self.options.append(
                _Option(value=value, text=text or value, selected=self._option.selected)
            )
            self._option = None
            self._option_text = []
        elif (
            tag == "td"
            and self._table is not None
            and self._table.depth == 1
            and self._cell is not None
        ):
            self._row.append(_normalize_text("".join(self._cell)))
            self._cell = None
        elif tag == "tr" and self._table is not None and self._table.depth == 1:
            if self._row:
                self._table.rows.append(self._row)
            self._row = []
        elif tag == "table" and self._table is not None:
            self._table.depth -= 1
            if self._table.depth == 0:
                table_id = self._table.id
                if table_id in self.tables:
                    self.duplicate_tables.add(table_id)
                self.tables[table_id] = self._table.rows
                self._table = None


def _parse_page(body: str) -> _PageParser:
    parser = _PageParser()
    try:
        parser.feed(body)
        parser.close()
    except Exception:
        raise _invalid("学校电费页面结构无法解析。") from None
    return parser


def parse_form(body: str) -> PowerForm:
    """Parse the public query form and its live building-option list."""
    parser = _parse_page(body)
    if not parser.has_form1:
        if parser.wechat_redirect:
            raise ElectricityError(
                "auth_required",
                "学校电费页面要求微信授权；本工具不绕过授权，未查询电量。",
            )
        raise _invalid("学校电费页面结构已变化，未找到查询表单。")
    view_state = parser.hidden.get("__VIEWSTATE", "").strip()
    event_validation = parser.hidden.get("__EVENTVALIDATION", "").strip()
    if not view_state or not event_validation:
        raise _invalid("学校电费页面缺少查询表单校验字段。")
    generator = parser.hidden.get("__VIEWSTATEGENERATOR", "").strip()
    options: list[BuildingOption] = []
    seen: dict[str, str] = {}
    for option in parser.options:
        building_id = option.value.strip()
        if not building_id:
            continue
        if len(building_id) > MAX_BUILDING_LENGTH or _has_control(building_id):
            raise _invalid("学校返回的楼号选项格式异常。")
        name = option.text.strip() or building_id
        if len(name) > MAX_BUILDING_LENGTH or _has_control(name):
            raise _invalid("学校返回的楼号名称格式异常。")
        if building_id in seen:
            if seen[building_id] != name:
                raise _invalid("学校返回了重复且不一致的楼号选项。")
            continue
        seen[building_id] = name
        options.append(BuildingOption(id=building_id, name=name))
    if not options:
        raise _invalid("学校电费页面没有返回楼号列表。")
    return PowerForm(
        view_state=view_state,
        view_state_generator=generator,
        event_validation=event_validation,
        buildings=tuple(options),
    )


def _grid_cells(parser: _PageParser) -> list[str] | None:
    rows = parser.tables.get("yongdianGrid")
    if rows is None:
        return None
    return [cell for row in rows for cell in row]


def _reading_from_grid(cells: list[str], requested_building: str) -> Reading:
    label_index = next(
        (index for index, cell in enumerate(cells) if "当前剩余电量" in cell),
        None,
    )
    if label_index is None or label_index + 1 >= len(cells):
        raise _invalid("学校电费页面已变化，未能读到剩余电量。")
    kwh = parse_kwh(cells[label_index + 1])
    hidden = [cell for cell in cells[label_index + 2 :] if cell]
    if len(hidden) != 2:
        raise _invalid("学校电费页面缺少系统编号或房间归属字段。")
    system_no, room = hidden
    if not system_no or not room:
        raise _invalid("学校电费页面返回了空的系统编号或房间号。")
    if len(system_no) > 80 or _has_control(system_no):
        raise _invalid("学校返回的系统编号格式异常。")
    if len(room) > MAX_ROOM_LENGTH or _has_control(room):
        raise _invalid("学校返回的房间号格式异常。")
    return Reading(
        building=requested_building,
        room=room,
        system_no=system_no,
        kwh=kwh,
    )


def _candidates_from_table(parser: _PageParser) -> tuple[Candidate, ...]:
    rows = parser.tables.get("yonghuGridView")
    if rows is None:
        return ()
    candidates: list[Candidate] = []
    for cells in rows:
        if len(cells) < 3:
            continue
        if any("系统编号" in cell for cell in cells):
            continue
        system_no = cells[0].strip()
        building = cells[1].strip()
        room = cells[2].strip()
        if not system_no and not building and not room:
            continue
        if not system_no or not building or not room:
            raise _invalid("学校返回的候选房间表字段不完整。")
        if (
            len(system_no) > 80
            or len(building) > MAX_BUILDING_LENGTH
            or len(room) > MAX_ROOM_LENGTH
            or _has_control(system_no)
            or _has_control(building)
            or _has_control(room)
        ):
            raise _invalid("学校返回的候选房间字段格式异常。")
        candidates.append(Candidate(system_no=system_no, building=building, room=room))
    return tuple(candidates)


def parse_result(
    body: str,
    requested_building: str,
    requested_room: str,
) -> Reading | Ambiguous:
    """Parse one query result and enforce exact room ownership.

    A candidate table without a unique exact building/room match returns
    ``Ambiguous``.  A grid reading for another room is also ambiguous, never a
    success and never a zero reading.  A missing result is ``room_not_found``.
    """
    parser = _parse_page(body)
    if parser.wechat_redirect and not parser.has_form1:
        raise ElectricityError(
            "auth_required",
            "学校电费页面要求微信授权；本工具不绕过授权，未查询电量。",
        )
    markers = {match.group("id") for match in TABLE_ID_RE.finditer(body)}
    if not parser.has_form1 and not parser.tables:
        raise _invalid("学校电费页面结构已变化，未找到查询表单或结果。")
    for table_id in markers:
        if table_id not in parser.tables:
            raise _invalid("学校电费页面结果表结构不完整。")
    if parser.duplicate_tables:
        raise _invalid("学校电费页面返回了重复的结果表。")

    candidates = _candidates_from_table(parser)
    cells = _grid_cells(parser)
    grid: Reading | None = None
    grid_error: ElectricityError | None = None
    if cells is not None:
        try:
            grid = _reading_from_grid(cells, requested_building)
        except ElectricityError as error:
            if error.code != "schema_changed":
                raise
            grid_error = error

    if candidates:
        exact = [
            candidate
            for candidate in candidates
            if candidate.building == requested_building
            and candidate.room == requested_room
        ]
        if len(exact) == 1 and grid is not None:
            candidate = exact[0]
            if (
                grid.room == requested_room
                and grid.system_no == candidate.system_no
            ):
                return Reading(
                    building=requested_building,
                    room=candidate.room,
                    system_no=candidate.system_no,
                    kwh=grid.kwh,
                )
            return Ambiguous(
                candidates=candidates,
                reason="reading_ownership_mismatch",
            )
        if len(exact) == 1:
            reason = "reading_unavailable" if grid_error else "selection_required"
        elif len(exact) > 1:
            reason = "multiple_exact_candidates"
        else:
            reason = "no_exact_candidate"
        return Ambiguous(candidates=candidates, reason=reason)

    if grid_error is not None:
        raise grid_error
    if grid is None:
        raise ElectricityError(
            "room_not_found",
            "学校未返回该房间的剩余电量记录；请核对楼号与房间号。",
            details={
                "query": {
                    "campus": CAMPUS,
                    "building": requested_building,
                    "room": requested_room,
                }
            },
        )
    if grid.room != requested_room:
        return Ambiguous(
            candidates=(
                Candidate(
                    system_no=grid.system_no,
                    building=requested_building,
                    room=grid.room,
                ),
            ),
            reason="room_mismatch",
        )
    return grid


def parse_kwh(raw: object) -> Decimal:
    """Parse the observed ``<decimal>度`` field without accepting money/units."""
    if not isinstance(raw, str):
        raise _invalid("学校返回的剩余电量字段不是文本。")
    text = _normalize_text(raw)
    match = KWH_RE.fullmatch(text)
    if match is None:
        raise _invalid("学校返回的剩余电量不再是非负的“度”数值。")
    try:
        value = Decimal(match.group("number"))
    except (InvalidOperation, ValueError):
        raise _invalid("学校返回的剩余电量不是有效数字。") from None
    if not value.is_finite() or value < 0:
        raise _invalid("学校返回的剩余电量不是有效的非负数值。")
    return value


def format_kwh(value: Decimal) -> str:
    """Serialize a Decimal without float conversion or scientific notation."""
    return format(value, "f")


def build_form_body(form: PowerForm, building: str, room: str) -> str:
    """Build the exact observed WebForms body in stable field order."""
    return urlencode(
        [
            ("__VIEWSTATE", form.view_state),
            ("__VIEWSTATEGENERATOR", form.view_state_generator),
            ("__EVENTVALIDATION", form.event_validation),
            ("lou", building),
            ("yonghu", room),
            ("chaxun", "查询"),
        ],
        encoding="utf-8",
        errors="strict",
    )


def resolve_building(form: PowerForm, building: str) -> str:
    """Require the caller's building to be one current school option id."""
    for option in form.buildings:
        if option.id == building:
            return building
    raise ElectricityError(
        "invalid_building",
        "学校当前楼号目录中没有该楼号；请先调用 upc_power_buildings 获取 id，"
        "未替换或猜测楼号。",
        details={
            "building": building,
            "building_count": len(form.buildings),
            "available_buildings": [option.id for option in form.buildings],
        },
    )


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


class ElectricityClient:
    """HTTP adapter for the one fixed anonymous Tangdaowan form endpoint."""

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: httpx.Timeout | None = None,
    ):
        self.transport = transport
        self.timeout = timeout or REQUEST_TIMEOUT

    async def _request(
        self,
        method: str,
        url: str,
        *,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> str:
        body = bytearray()
        try:
            # A fresh client per request means no cookie persistence and no
            # keep-alive pool crossing GET/POST or separate queries.
            async with httpx.AsyncClient(
                transport=self.transport,
                trust_env=False,
                follow_redirects=False,
                timeout=self.timeout,
                limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
            ) as client:
                async with client.stream(
                    method, url, content=content, headers=headers
                ) as response:
                    status = response.status_code
                    if 300 <= status < 400:
                        location = response.headers.get("location", "").lower()
                        if "open.weixin.qq.com" in location:
                            raise ElectricityError(
                                "auth_required",
                                "学校电费页面要求微信授权；本工具不绕过授权，未查询电量。",
                            )
                        raise ElectricityError(
                            "redirect",
                            "学校电费页面要求跳转，本工具不跟随重定向。",
                            details={"http_status": status},
                        )
                    if status in (401, 403):
                        raise ElectricityError(
                            "access_denied",
                            "学校电费页面拒绝了匿名请求，未继续重试。",
                            details={"http_status": status},
                        )
                    if status == 429:
                        raise ElectricityError(
                            "rate_limited",
                            "学校电费页面暂时限流，未继续重试。",
                            details={"http_status": status},
                        )
                    if not 200 <= status <= 299:
                        raise ElectricityError(
                            "upstream_error",
                            f"学校电费页面返回 HTTP {status}。",
                            details={"http_status": status},
                        )
                    declared = response.headers.get("content-length")
                    if declared is not None:
                        try:
                            declared_size = int(declared)
                        except ValueError:
                            declared_size = None
                        if declared_size is not None and declared_size > MAX_RESPONSE_BYTES:
                            raise ElectricityError(
                                "response_too_large",
                                "学校电费页面响应超过本工具 1 MiB 上限。",
                            )
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise ElectricityError(
                                "response_too_large",
                                "学校电费页面响应超过本工具 1 MiB 上限。",
                            )
                        body.extend(chunk)
        except ElectricityError:
            raise
        except httpx.TimeoutException:
            raise ElectricityError(
                "timeout", "学校电费页面响应超时，未自动重试。"
            ) from None
        except httpx.DecodingError:
            raise _invalid("学校电费页面响应编码无法解析。") from None
        except (httpx.HTTPError, OSError) as error:
            if _is_tls_error(error):
                raise ElectricityError(
                    "tls_error",
                    "学校电费服务的 TLS 校验失败；未降级证书校验。",
                ) from None
            raise ElectricityError(
                "network_error", "无法连接学校电费页面，请检查网络后重试。"
            ) from None
        try:
            return bytes(body).decode("utf-8-sig")
        except UnicodeDecodeError:
            raise _invalid("学校电费页面响应编码无法解析。") from None

    async def fetch_form(self) -> PowerForm:
        page = await self._request("GET", GET_URL, headers=REQUEST_HEADERS)
        return parse_form(page)

    async def fetch_buildings(self) -> tuple[BuildingOption, ...]:
        return (await self.fetch_form()).buildings

    async def query(self, building: object, room: object) -> Reading | Ambiguous:
        building_value, room_value = validate_input(building, room)
        form = await self.fetch_form()
        resolved = resolve_building(form, building_value)
        page = await self._request(
            "POST",
            POST_URL,
            content=build_form_body(form, resolved, room_value).encode("utf-8"),
            headers=FORM_HEADERS,
        )
        return parse_result(page, resolved, room_value)
