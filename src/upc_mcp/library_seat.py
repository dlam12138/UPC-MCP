"""Strict protocol adapter for the UPC library seat system.

Only the observed ``https://libspace.upc.edu.cn`` business endpoints are used.
The public signing configuration is decoded from the values shipped by the
school frontend; no user token or cookie is ever logged or included in an
exception message.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import ssl
import time
from dataclasses import dataclass
from datetime import date as Date
from typing import Any, Literal
from urllib.parse import quote, urlencode
from uuid import uuid4

import httpx

BASE_URL = "https://libspace.upc.edu.cn"
BASE_HOST = "libspace.upc.edu.cn"
LOGIN_URL = (
    f"{BASE_URL}/remote/static/sso/login"
    "?redirectUrl=https%3A%2F%2Flibspace.upc.edu.cn%2Flibseat%2F%23%2Flogin"
)
SIGNING_KEY_ID = b"server_date_time"
SIGNING_IV = b"client_date_time"
SIGNING_CIPHERTEXT = "cDf+jadFUWncEn536MXItw=="
DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_CONCURRENCY = 4
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36 Edg/156.0.0.0"
)
SAFE_ID = re.compile(r"^[A-Za-z0-9._~-]{1,128}$")
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
ACTIVE_RESERVATION_STATUS = {"RESERVE", "CHECK_IN", "AWAY"}
TERMINAL_RESERVATION_STATUS = {"COMPLETE", "INCOMPLETE", "MISS", "CANCEL", "STOP"}


class LibraryError(Exception):
    """Fixed, credential-free error crossing the service/CLI/MCP boundary."""

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None):
        self.code = code
        self.details = details or {}
        super().__init__(message)

    def payload(self) -> dict:
        payload = {
            "status": self.code,
            "source_system": "library",
            "error": {"message": str(self)},
        }
        if self.details:
            payload["error"].update(self.details)
        return payload


def signing_key() -> bytes:
    """Decode the public AES-CBC/PKCS7 configuration from the school page."""
    try:
        from cryptography.hazmat.primitives import padding
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        raw = _b64decode(SIGNING_CIPHERTEXT)
        decryptor = Cipher(algorithms.AES(SIGNING_KEY_ID), modes.CBC(SIGNING_IV)).decryptor()
        padded = decryptor.update(raw) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        key = unpadder.update(padded) + unpadder.finalize()
    except Exception:
        raise LibraryError("local_error", "本地图书馆签名配置解码失败。") from None
    if not 1 <= len(key) <= 64:
        raise LibraryError("local_error", "本地图书馆签名配置长度异常。")
    return key


def _b64decode(value: str) -> bytes:
    import base64

    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        raise LibraryError("local_error", "本地图书馆签名配置格式异常。") from None


_SIGNING_KEY: bytes | None = None


def _key() -> bytes:
    global _SIGNING_KEY
    if _SIGNING_KEY is None:
        _SIGNING_KEY = signing_key()
    return _SIGNING_KEY


def sign(method: str, request_id: str, timestamp: int, key: bytes | None = None) -> str:
    method = method.upper()
    if method not in {"GET", "POST"}:
        raise LibraryError("invalid_input", "不支持的图书馆请求方法。")
    import hashlib
    import hmac

    message = f"seat::{request_id}::{timestamp}::{method}".encode()
    return hmac.new(key or _key(), message, hashlib.sha256).hexdigest()


def signed_headers(
    token: str | None,
    method: str,
    *,
    request_id: str | None = None,
    timestamp: int | None = None,
    key: bytes | None = None,
) -> dict[str, str]:
    method = method.upper()
    request_id = request_id or str(uuid4())
    timestamp = timestamp if timestamp is not None else int(time.time() * 1000)
    headers = {
        "loginType": "PC",
        "X-request-id": request_id,
        "X-request-date": str(timestamp),
        "X-hmac-request-key": sign(method, request_id, timestamp, key),
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    if token:
        # The school frontend sends the raw business token, not a Bearer value.
        headers["Authorization"] = token
    return headers


def safe_id(value: str, field: str) -> str:
    if (
        not isinstance(value, str)
        or not SAFE_ID.fullmatch(value)
        or value in {".", ".."}
    ):
        raise LibraryError("invalid_input", f"{field} 必须是学校返回的 ID。")
    return value


def safe_date(value: str) -> str:
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        raise LibraryError("invalid_input", "date 必须是显式 YYYY-MM-DD。")
    try:
        Date.fromisoformat(value)
    except ValueError:
        raise LibraryError("invalid_input", "date 不是有效日期。") from None
    return value


def identity_hash(user: LibraryUser) -> str:
    raw = f"libseat\0{user.id}\0{user.username}".encode()
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class LibraryUser:
    id: str
    username: str
    name: str

    def public(self) -> dict:
        return {"identity_verified": bool(self.id and self.username)}


@dataclass(frozen=True)
class Building:
    id: str
    name: str


@dataclass(frozen=True)
class Floor:
    floor_num: int
    floor_name: str


@dataclass(frozen=True)
class Room:
    room_id: str
    name: str
    floor: int
    floor_name: str | None = None


@dataclass(frozen=True)
class Seat:
    id: str
    name: str | None
    row: int
    col: int
    type: str
    enabled: bool
    status: str
    local_only: bool

    @property
    def is_seat(self) -> bool:
        return self.type.lower() == "seat"

    @property
    def selectable(self) -> bool:
        return (
            self.is_seat
            and self.enabled
            and not self.local_only
            and self.status in {"FREE", "YESSELECT"}
        )

    def public(self) -> dict:
        return {
            "seat_id": self.id,
            "name": self.name,
            "row": self.row,
            "col": self.col,
            "type": self.type,
            "enabled": self.enabled,
            "status": self.status,
            "local_only": self.local_only,
            "selectable": self.selectable,
        }


@dataclass(frozen=True)
class Layout:
    rows: int
    cols: int
    seats: tuple[Seat, ...]


@dataclass(frozen=True)
class TimeOption:
    id: str
    value: str

    def public(self) -> dict:
        return {"id": self.id, "value": self.value}


@dataclass(frozen=True)
class Reservation:
    id: str
    date: str
    begin: str
    end: str
    location: str
    status: str
    seat_id: str | None = None
    room_id: str | None = None

    def public(self) -> dict:
        return {
            "reservation_id": self.id,
            "date": self.date,
            "begin": self.begin,
            "end": self.end,
            "location": self.location,
            "status": self.status,
            "seat_id": self.seat_id,
            "room_id": self.room_id,
        }


@dataclass(frozen=True)
class ReservationResult:
    items: tuple[Reservation, ...]
    empty_state: Literal["data_null", "array_empty"] | None


@dataclass(frozen=True)
class Settings:
    captcha_required: bool | None


def _reject_constant(value: str) -> None:
    raise ValueError(value)


def parse_object(body: str) -> dict:
    try:
        root = json.loads(body, parse_constant=_reject_constant)
    except (ValueError, TypeError, RecursionError):
        raise LibraryError("schema_changed", "学校返回格式已变化，未解析出 JSON 对象。") from None
    if not isinstance(root, dict):
        raise LibraryError("schema_changed", "学校返回格式已变化，顶层不是对象。")
    return root


def _int_value(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _text(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return None


def _bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value == 1
    if isinstance(value, str):
        return value == "1" or value.lower() == "true"
    return False


def ensure_success(root: dict) -> None:
    code = _int_value(root.get("code"))
    if code == 12:
        raise LibraryError("auth_expired", "图书馆座位登录已失效，请重新完成学校登录。")
    if code == 20:
        raise LibraryError(
            "upstream_error",
            "学校拒绝了这次请求；本地判断为签名或时间不匹配，不是预约功能停用。",
            details={"upstream_code": 20},
        )
    status = root.get("status")
    if isinstance(status, bool) and not status:
        raise LibraryError(
            "business_rejected",
            "学校返回操作失败。",
            details={"upstream_code": code} if code is not None else None,
        )
    if isinstance(status, str) and status.lower() in {"fail", "error", "false"}:
        raise LibraryError(
            "business_rejected",
            "学校返回操作失败。",
            details={"upstream_code": code} if code is not None else None,
        )
    success = root.get("success")
    if isinstance(success, bool) and not success:
        raise LibraryError(
            "business_rejected",
            "学校返回操作失败。",
            details={"upstream_code": code} if code is not None else None,
        )
    if code in {0, 200}:
        return
    if code is not None:
        raise LibraryError(
            "upstream_unknown",
            "学校返回了未识别的业务码。",
            details={"upstream_code": code},
        )
    if (
        "data" in root
        or success is True
        or isinstance(status, str)
        and status.lower() in {"success", "true"}
    ):
        return
    raise LibraryError("schema_changed", "学校返回格式已变化，缺少可识别的业务状态。")


def _data_object(root: dict, label: str) -> dict:
    data = root.get("data")
    if not isinstance(data, dict):
        raise LibraryError("schema_changed", f"{label}响应缺少 data 对象。")
    return data


def _data_array(root: dict, label: str) -> list:
    data = root.get("data")
    if not isinstance(data, list):
        raise LibraryError("schema_changed", f"{label}响应缺少 data 数组。")
    return data


def parse_user(body: str) -> LibraryUser:
    root = parse_object(body)
    ensure_success(root)
    data = _data_object(root, "用户信息")
    user_id = _text(data.get("id"))
    username = _text(data.get("username"))
    if not user_id or not username:
        raise LibraryError("schema_changed", "用户信息缺少稳定本人标识。")
    name = _text(data.get("name")) or username
    return LibraryUser(id=user_id, username=username, name=name)


def parse_exchange_token(body: str) -> str:
    root = parse_object(body)
    ensure_success(root)
    data = _data_object(root, "令牌兑换")
    token = _text(data.get("token"))
    if not token or not token.strip():
        raise LibraryError("schema_changed", "令牌兑换响应缺少 token。")
    return token


def parse_filters(body: str) -> tuple[list[str], list[Building]]:
    root = parse_object(body)
    ensure_success(root)
    data = _data_object(root, "筛选条件")
    raw_dates = data.get("dates")
    raw_buildings = data.get("buildings")
    if not isinstance(raw_dates, list) or not isinstance(raw_buildings, list):
        raise LibraryError("schema_changed", "筛选条件缺少 dates 或 buildings 数组。")
    dates: list[str] = []
    for item in raw_dates:
        value = _text(item)
        if not value or not DATE_RE.fullmatch(value):
            raise LibraryError("schema_changed", "可预约日期格式异常。")
        if value not in dates:
            dates.append(value)
    buildings: list[Building] = []
    seen: dict[str, str] = {}
    for item in raw_buildings:
        if isinstance(item, (list, tuple)):
            if len(item) < 2:
                raise LibraryError("schema_changed", "馆区字段不完整。")
            building_id, name = _text(item[0]), _text(item[1])
        elif isinstance(item, dict):
            building_id, name = _text(item.get("id")), _text(item.get("name"))
        else:
            raise LibraryError("schema_changed", "馆区字段格式异常。")
        if not building_id or not name:
            raise LibraryError("schema_changed", "馆区字段不完整。")
        previous = seen.get(building_id)
        if previous is not None and previous != name:
            raise LibraryError("schema_changed", "馆区 ID 对应了多个名称。")
        if previous is None:
            seen[building_id] = name
            buildings.append(Building(building_id, name))
    return dates, buildings


def parse_floors(body: str) -> list[Floor]:
    root = parse_object(body)
    ensure_success(root)
    data = _data_array(root, "楼层")
    floors: list[Floor] = []
    for item in data:
        if not isinstance(item, dict):
            raise LibraryError("schema_changed", "楼层字段格式异常。")
        number = _int_value(item.get("floorNum"))
        name = _text(item.get("floorName"))
        if number is None or name is None:
            raise LibraryError("schema_changed", "楼层字段不完整。")
        floors.append(Floor(number, name))
    return floors


def parse_rooms(body: str) -> list[Room]:
    root = parse_object(body)
    ensure_success(root)
    data = _data_array(root, "房间")
    rooms: list[Room] = []
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            raise LibraryError("schema_changed", "房间字段格式异常。")
        room_id = _text(item.get("roomId")) or _text(item.get("id"))
        name = (
            _text(item.get("room"))
            or _text(item.get("roomName"))
            or _text(item.get("name"))
        )
        floor = _int_value(item.get("floor"))
        if not room_id or not name or floor is None:
            raise LibraryError("schema_changed", "房间字段不完整。")
        if room_id in seen:
            continue
        seen.add(room_id)
        rooms.append(Room(room_id=room_id, name=name, floor=floor))
    return rooms


def parse_layout(body: str) -> Layout:
    root = parse_object(body)
    ensure_success(root)
    data = _data_object(root, "座位布局")
    rows = _int_value(data.get("rows"))
    cols = _int_value(data.get("cols"))
    if rows is None or cols is None or not 1 <= rows <= 200 or not 1 <= cols <= 200:
        raise LibraryError("schema_changed", "座位布局尺寸异常。")
    layout = data.get("layout")
    if not isinstance(layout, dict):
        raise LibraryError("schema_changed", "座位布局缺少 layout 对象。")
    seats: list[Seat] = []
    seen_ids: set[str] = set()
    for key, value in layout.items():
        if not isinstance(value, dict):
            continue
        numeric_key = _int_value(key)
        row = _int_value(value.get("row"))
        col = _int_value(value.get("col"))
        if row is None and numeric_key is not None:
            row = numeric_key // 1000
        if col is None and numeric_key is not None:
            col = numeric_key % 1000
        if row is None or col is None:
            parts = str(key).split("_", 1)
            if len(parts) == 2:
                row = _int_value(parts[0]) if row is None else row
                col = _int_value(parts[1]) if col is None else col
        if row is None or col is None:
            continue
        seat_id = _text(value.get("id"))
        if not seat_id:
            continue
        if seat_id in seen_ids:
            raise LibraryError("schema_changed", "座位布局中座位 ID 重复。")
        seen_ids.add(seat_id)
        seat_type = _text(value.get("type")) or "seat"
        seat_status = _text(value.get("status")) or "UNKNOWN"
        seats.append(
            Seat(
                id=seat_id,
                name=_text(value.get("name")),
                row=row,
                col=col,
                type=seat_type,
                enabled=_bool_value(value.get("enabled")),
                status=seat_status,
                local_only=_bool_value(value.get("local")),
            )
        )
    if not seats:
        raise LibraryError("schema_changed", "学校未返回可用座位布局。")
    return Layout(rows=rows, cols=cols, seats=tuple(seats))


def parse_times(body: str, key: str) -> list[TimeOption]:
    root = parse_object(body)
    ensure_success(root)
    data = _data_object(root, "时间选项")
    raw = data.get(key)
    if not isinstance(raw, list):
        raise LibraryError("schema_changed", f"学校未返回 {key} 数组。")
    options: list[TimeOption] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise LibraryError("schema_changed", "时间字段格式异常。")
        option_id = _text(item.get("id"))
        if not option_id:
            raise LibraryError("schema_changed", "时间选项缺少 id。")
        value = _text(item.get("value")) or option_id
        if option_id in seen:
            raise LibraryError("schema_changed", "时间选项 ID 重复。")
        seen.add(option_id)
        options.append(TimeOption(option_id, value))
    return options


def parse_settings(body: str) -> Settings:
    root = parse_object(body)
    ensure_success(root)
    data = _data_object(root, "设置")
    if "isCaptchaOpen" not in data:
        return Settings(captcha_required=None)
    return Settings(captcha_required=_bool_value(data.get("isCaptchaOpen")))


def parse_reservations(body: str) -> ReservationResult:
    root = parse_object(body)
    ensure_success(root)
    if "data" not in root:
        raise LibraryError("schema_changed", "当前预约响应缺少 data 字段。")
    data = root["data"]
    if data is None:
        return ReservationResult(items=(), empty_state="data_null")
    if not isinstance(data, list):
        raise LibraryError("schema_changed", "当前预约 data 既不是数组也不是 null。")
    items: list[Reservation] = []
    for item in data:
        if not isinstance(item, dict):
            raise LibraryError("schema_changed", "预约记录不是对象。")
        reservation_id = _text(item.get("id"))
        reservation_date = _text(item.get("date")) or _text(item.get("onDate"))
        begin = _text(item.get("begin"))
        end = _text(item.get("end"))
        location = _text(item.get("location")) or _text(item.get("loc"))
        if not reservation_id or not reservation_date or not begin or not end or not location:
            raise LibraryError("schema_changed", "预约记录缺少稳定字段。")
        items.append(
            Reservation(
                id=reservation_id,
                date=reservation_date,
                begin=begin,
                end=end,
                location=location,
                status=_text(item.get("stat")) or _text(item.get("status")) or "",
                # Only exact, observed field names are accepted; absence is unknown.
                seat_id=_text(item.get("seatId")),
                room_id=_text(item.get("roomId")),
            )
        )
    return ReservationResult(
        items=tuple(items),
        empty_state=None if items else "array_empty",
    )


def minutes(value: str) -> int | None:
    parts = value.strip().split(":")
    if len(parts) < 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return None
    return hour * 60 + minute


def reservation_active(status: str) -> bool | None:
    """True/False only for observed statuses; unknown is not treated as either."""
    value = status.strip().upper()
    if value in ACTIVE_RESERVATION_STATUS:
        return True
    if value in TERMINAL_RESERVATION_STATUS:
        return False
    return None


def _bounded_token(text: str, token: str, *, seat: bool) -> re.Match[str] | None:
    if not text or not token:
        return None
    # A room may be followed by a seat separator ("3F-A01"). A seat may not be
    # followed by another seat-like suffix ("A01-1", "A01_1", "A01.1", "A010").
    after = (
        r"(?![0-9A-Za-z\u4e00-\u9fff._-])"
        if seat
        else r"(?![0-9A-Za-z\u4e00-\u9fff])"
    )
    pattern = (
        r"(?<![0-9A-Za-z\u4e00-\u9fff])"
        + re.escape(token.casefold())
        + after
    )
    return re.search(pattern, text.casefold())


def _location_has_room_and_seat(location: str, room_name: str, seat_name: str) -> bool:
    if room_name.casefold() == seat_name.casefold():
        return False
    room_match = _bounded_token(location, room_name, seat=False)
    seat_match = _bounded_token(location, seat_name, seat=True)
    if room_match is None or seat_match is None:
        return False
    return room_match.start() < seat_match.start()


def reservation_matches(
    reservation: Reservation,
    *,
    date: str,
    begin: str,
    end: str,
    seat_name: str | None,
    seat_id: str | None = None,
    room_id: str | None = None,
    room_name: str | None = None,
    room_name_unique: bool = False,
    seat_name_unique: bool = False,
) -> tuple[bool, str | None]:
    if reservation.date.strip() != date:
        return False, None
    expected_begin, expected_end = minutes(begin), minutes(end)
    actual_begin, actual_end = minutes(reservation.begin), minutes(reservation.end)
    if None in {expected_begin, expected_end, actual_begin, actual_end}:
        return False, None
    if actual_begin != expected_begin or actual_end != expected_end:
        return False, None

    reservation_seat = (reservation.seat_id or "").strip()
    reservation_room = (reservation.room_id or "").strip()
    target_seat = (seat_id or "").strip()
    target_room = (room_id or "").strip()
    room_name = room_name.strip() if room_name else None
    seat_name = seat_name.strip() if seat_name else None

    # Strongest evidence: both stable IDs returned by the reservation API.
    if reservation_seat and reservation_room:
        if not target_seat or not target_room:
            return False, None
        if reservation_seat != target_seat or reservation_room != target_room:
            return False, None
        return True, "seat_room_id"

    if reservation_seat:
        if not target_seat or reservation_seat != target_seat:
            return False, None
        if not room_name or not room_name_unique:
            return False, None
        if not seat_name or not seat_name_unique:
            return False, None
        if not _location_has_room_and_seat(reservation.location, room_name, seat_name):
            return False, None
        return True, "seat_id_location"

    if reservation_room:
        if not target_room or reservation_room != target_room:
            return False, None
        if not seat_name or not seat_name_unique:
            return False, None
        if not _bounded_token(reservation.location, seat_name, seat=True):
            return False, None
        return True, "room_id_location"

    # Text-only fallback requires a unique room name and a unique seat name.
    if not room_name or not seat_name:
        return False, None
    if not room_name_unique or not seat_name_unique:
        return False, None
    if not _location_has_room_and_seat(reservation.location, room_name, seat_name):
        return False, None
    return True, "location"


def overlaps(
    reservation: Reservation, *, date: str, begin: str, end: str
) -> bool | None:
    if reservation.date.strip() != date:
        return False
    if reservation_active(reservation.status) is False:
        return False
    left, right = minutes(begin), minutes(end)
    other_left, other_right = minutes(reservation.begin), minutes(reservation.end)
    if None in {left, right, other_left, other_right}:
        return None
    return left < other_right and other_left < right


class LibrarySeatClient:
    """Async transport for the fixed library endpoints; never retries."""

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        timeout: httpx.Timeout | None = None,
        max_bytes: int = MAX_RESPONSE_BYTES,
        concurrency: int = MAX_CONCURRENCY,
    ):
        self.transport = transport
        self.timeout = timeout or DEFAULT_TIMEOUT
        self.max_bytes = max_bytes
        self.concurrency = max(1, concurrency)
        self._semaphore: asyncio.Semaphore | None = None
        self._semaphore_loop: asyncio.AbstractEventLoop | None = None

    def _limit(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        if self._semaphore is None or self._semaphore_loop is not loop:
            self._semaphore = asyncio.Semaphore(self.concurrency)
            self._semaphore_loop = loop
        return self._semaphore

    async def _request(
        self,
        method: str,
        path: str,
        *,
        token: str | None,
        query: dict[str, str] | None = None,
        form: dict[str, str] | None = None,
    ) -> str:
        if not path.startswith("/rest/"):
            raise LibraryError("local_error", "图书馆请求路径不在固定业务范围内。")
        params = dict(query or {})
        if token:
            params["token"] = token
        url = BASE_URL + path
        if params:
            url += "?" + urlencode(params)
        headers = signed_headers(token, method)
        if form is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded;charset=UTF-8"
        kwargs: dict[str, Any] = {
            "headers": headers,
            "follow_redirects": False,
            "timeout": self.timeout,
            "trust_env": False,
            "limits": httpx.Limits(
                max_connections=max(1, self.concurrency),
                max_keepalive_connections=min(2, max(1, self.concurrency)),
            ),
        }
        if self.transport is not None:
            kwargs["transport"] = self.transport
        async with self._limit():
            try:
                async with httpx.AsyncClient(**kwargs) as client:
                    async with client.stream(
                        method, url, data=form if form is not None else None
                    ) as response:
                        status = response.status_code
                        if status in {401, 403}:
                            raise LibraryError(
                                "auth_expired",
                                "图书馆座位登录已失效，请重新完成学校登录。",
                            )
                        if status in {301, 302, 303, 307, 308}:
                            raise LibraryError(
                                "auth_expired",
                                "图书馆座位服务要求跳转，登录可能已失效。",
                            )
                        if not 200 <= status <= 299:
                            raise LibraryError(
                                "upstream_error",
                                "图书馆座位服务暂时不可用。",
                                details={"http_status": status},
                            )
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(body) + len(chunk) > self.max_bytes:
                                raise LibraryError(
                                    "upstream_error",
                                    "图书馆座位响应超过本地大小限制。",
                                    details={"response_too_large": True},
                                )
                            body.extend(chunk)
            except LibraryError:
                raise
            except httpx.TimeoutException:
                raise LibraryError(
                    "network_error",
                    "图书馆座位请求超时。",
                    details={"timeout": True},
                ) from None
            except httpx.ConnectError as error:
                if isinstance(error.__cause__, ssl.SSLError):
                    raise LibraryError("tls_error", "图书馆座位 TLS 连接失败。") from None
                raise LibraryError("network_error", "无法连接图书馆座位系统。") from None
            except httpx.HTTPError:
                raise LibraryError("network_error", "图书馆座位网络请求失败。") from None
        try:
            return body.decode("utf-8")
        except UnicodeDecodeError:
            raise LibraryError("schema_changed", "图书馆座位响应不是 UTF-8。") from None

    async def exchange_app_token(self, app_token: str) -> str:
        if not app_token or not app_token.strip():
            raise LibraryError("invalid_input", "登录令牌为空。")
        body = await self._request(
            "POST",
            "/rest/ssoAuth",
            token=app_token,
            query={"token": app_token},
            form={"token": app_token},
        )
        return parse_exchange_token(body)

    async def user(self, token: str) -> LibraryUser:
        return parse_user(await self._request("GET", "/rest/v2/user", token=token))

    async def filters(self, token: str) -> tuple[list[str], list[Building]]:
        return parse_filters(
            await self._request("GET", "/rest/v2/free/filters", token=token)
        )

    async def floors(self, token: str, building_id: str) -> list[Floor]:
        safe_id(building_id, "building_id")
        path = f"/rest/v2/room/floors/{quote(building_id, safe='')}"
        return parse_floors(await self._request("POST", path, token=token, form={}))

    async def rooms(self, token: str, building_id: str, day: str) -> list[Room]:
        safe_id(building_id, "building_id")
        safe_date(day)
        path = (
            f"/rest/v2/room/stats2/{quote(building_id, safe='')}/"
            f"{quote(day, safe='')}"
        )
        return parse_rooms(await self._request("GET", path, token=token))

    async def layout(self, token: str, room_id: str, day: str) -> Layout:
        safe_id(room_id, "room_id")
        safe_date(day)
        path = (
            f"/rest/v2/room/layoutByDate/{quote(room_id, safe='')}/"
            f"{quote(day, safe='')}"
        )
        return parse_layout(await self._request("GET", path, token=token))

    async def start_times(self, token: str, seat_id: str, day: str) -> list[TimeOption]:
        safe_id(seat_id, "seat_id")
        safe_date(day)
        path = (
            f"/rest/v2/startTimesForSeat/{quote(seat_id, safe='')}/"
            f"{quote(day, safe='')}"
        )
        return parse_times(await self._request("GET", path, token=token), "startTimes")

    async def end_times(
        self, token: str, seat_id: str, day: str, start_id: str
    ) -> list[TimeOption]:
        safe_id(seat_id, "seat_id")
        safe_date(day)
        safe_id(start_id, "start_id")
        path = (
            f"/rest/v2/endTimesForSeat/{quote(seat_id, safe='')}/"
            f"{quote(day, safe='')}/{quote(start_id, safe='')}"
        )
        return parse_times(await self._request("GET", path, token=token), "endTimes")

    async def reservations(self, token: str) -> ReservationResult:
        return parse_reservations(
            await self._request("GET", "/rest/v2/user/reservations", token=token)
        )

    async def settings(self, token: str) -> Settings:
        return parse_settings(await self._request("GET", "/rest/v2/settings", token=token))

    async def free_book(
        self,
        token: str,
        *,
        user: LibraryUser,
        day: str,
        seat_id: str,
        start_id: str,
        end_id: str,
    ) -> None:
        safe_date(day)
        safe_id(seat_id, "seat_id")
        safe_id(start_id, "start_id")
        safe_id(end_id, "end_id")
        form = {
            "startTime": "-1" if start_id == "now" else start_id,
            "endTime": end_id,
            "seat": seat_id,
            "date": day,
            "userId": user.id,
            "username": user.username,
            "authid": "",
        }
        body = await self._request(
            "POST", "/rest/v2/freeBook", token=token, form=form
        )
        ensure_success(parse_object(body))
