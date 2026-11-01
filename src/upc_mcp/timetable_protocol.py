"""Strict parser and fixed-host client for the UPC campus-app timetable.

The only accepted host is ``app.upc.edu.cn``.  No caller can supply a base URL,
extra headers, HTTP method, cookie or user id.  The client reads the deployed
``weekschedule`` protocol:

* ``GET  /timetable/wap/default/get-index``
* ``POST /timetable/wap/default/get-datatmp`` with ``type=2``

Parsing rejects contradictions instead of silently repairing or dropping data.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import re
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

BASE_URL = "https://app.upc.edu.cn"
PAGE_PATH = "/site/weekschedule/index"
INDEX_PATH = "/timetable/wap/default/get-index"
WEEK_PATH = "/timetable/wap/default/get-datatmp"
ISO_WEEKDAY_LABELS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
WEEK_TYPE = "2"
MAX_WEEKS = 30
MAX_PERIOD = 14
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=15.0, pool=5.0)
TERM_LABELS = {"1": "秋季", "2": "春季", "3": "夏季"}
_COOKIE_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_COOKIE_VALUE = re.compile(r"^[\x21-\x7e]*$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class TimetableError(Exception):
    """Fixed, credential-free error that may cross the CLI/MCP boundary."""

    def __init__(self, code: str, message: str, *, action: str | None = None):
        self.code = code
        self.action = action
        super().__init__(message)

    def payload(self) -> dict:
        payload = {
            "status": self.code,
            "source_system": "timetable",
            "session_ref": "self",
            "error": {"message": str(self)},
        }
        if self.action:
            payload["error"]["action"] = self.action
        return payload


class TimetableCookie(BaseModel):
    """A browser cookie with its original scope attributes."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=256)
    value: SecretStr
    domain: str = Field(min_length=1, max_length=253)
    path: str = Field(default="/", min_length=1, max_length=1024)
    secure: bool = False
    http_only: bool = False
    same_site: Literal["Strict", "Lax", "None"] | None = None
    expires_at: float | None = None

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        if not _COOKIE_NAME.fullmatch(value):
            raise ValueError("invalid cookie name")
        return value

    @field_validator("value")
    @classmethod
    def _valid_value(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if len(raw) > 4096 or not _COOKIE_VALUE.fullmatch(raw):
            raise ValueError("invalid cookie value")
        return value

    @field_validator("domain")
    @classmethod
    def _valid_domain(cls, value: str) -> str:
        domain = value.strip().lower().rstrip(".")
        if (
            not domain
            or domain.startswith(".")
            and domain == "."
            or any(ch.isspace() or ord(ch) < 32 for ch in domain)
        ):
            raise ValueError("invalid cookie domain")
        base = domain.lstrip(".")
        if base not in {"upc.edu.cn"} and not base.endswith(".upc.edu.cn"):
            raise ValueError("cookie domain is outside upc.edu.cn")
        if base == "cas.upc.edu.cn":
            raise ValueError("CAS cookies are not timetable cookies")
        return domain

    @field_validator("path")
    @classmethod
    def _valid_path(cls, value: str) -> str:
        if not value.startswith("/") or any(ch in value for ch in "\r\n\x00"):
            raise ValueError("invalid cookie path")
        return value

    def matches(self, host: str, path: str, *, secure: bool) -> bool:
        host = host.lower().rstrip(".")
        domain = self.domain.lower().lstrip(".")
        if host != domain and not host.endswith("." + domain):
            return False
        if self.secure and not secure:
            return False
        if not self._path_matches(path):
            return False
        if self.expires_at is not None and self.expires_at <= time.time():
            return False
        return True

    def _path_matches(self, request_path: str) -> bool:
        if self.path == "/":
            return True
        if request_path == self.path:
            return True
        if not request_path.startswith(self.path):
            return False
        # RFC 6265 path-match: /site must not match /sitefoo.
        return self.path.endswith("/") or request_path[len(self.path)] == "/"

    def header_value(self) -> str:
        return f"{self.name}={self.value.get_secret_value()}"

    def secret_payload(self) -> dict:
        return {
            "name": self.name,
            "value": self.value.get_secret_value(),
            "domain": self.domain,
            "path": self.path,
            "secure": self.secure,
            "http_only": self.http_only,
            "same_site": self.same_site,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True)
class TimetableTerm:
    year: str
    term: str
    startday: dt.date
    total_weeks: int
    id: str | None
    label: str

    def public(self) -> dict:
        return {
            "year": self.year,
            "term": self.term,
            "label": self.label,
            "startday": self.startday.isoformat(),
            "total_weeks": self.total_weeks,
            "id": self.id,
        }


@dataclass(frozen=True)
class TimetableIndex:
    terms: tuple[TimetableTerm, ...]
    current_year: str | None
    current_term: str | None
    current_week: int | None
    current_weekdays: tuple[dt.date, ...] | None
    current_weekday: int | None

    def find(self, year: str, term: str) -> TimetableTerm | None:
        return next((item for item in self.terms if item.year == year and item.term == term), None)

    def current(self) -> TimetableTerm | None:
        if self.current_year is None or self.current_term is None:
            return None
        return self.find(self.current_year, self.current_term)


@dataclass(frozen=True)
class TimetableMeeting:
    course_name: str
    weekday: int
    start_period: int
    end_period: int
    periods: tuple[int, ...]
    source_periods: tuple[int, ...]
    weeks: tuple[int, ...]
    week_label: str
    teacher: str | None
    location: str | None
    course_id: str | None
    week_type: str | None
    order: int | None
    total_length: int | None
    lessons: str | None
    course_time: str | None
    sections_source: str
    credit: str | None = None
    course_type: str | None = None
    course_hour: str | None = None
    exam_type: str | None = None

    def public(self, day: dt.date) -> dict:
        instance_key = "|".join(
            [
                self.course_id or "",
                self.course_name,
                day.isoformat(),
                f"{self.start_period}-{self.end_period}",
                str(self.order if self.order is not None else ""),
                self.teacher or "",
                self.location or "",
            ]
        )
        return {
            "date": day.isoformat(),
            "weekday": self.weekday,
            "weekday_label": ISO_WEEKDAY_LABELS[self.weekday - 1],
            "teaching_day_index": day.isoweekday() % 7 + 1,
            "instance_id": hashlib.sha256(instance_key.encode("utf-8")).hexdigest()[:16],
            "course_name": self.course_name,
            "teacher": self.teacher,
            "location": self.location,
            "course_id": self.course_id,
            "periods": list(self.periods),
            "source_periods": list(self.source_periods),
            "start_period": self.start_period,
            "end_period": self.end_period,
            "weeks": list(self.weeks),
            "week_label": self.week_label,
            "week_type": self.week_type,
            "order": self.order,
            "total_length": self.total_length,
            "lessons": self.lessons,
            "course_time": self.course_time,
            "time_range": self.course_time,
            "sections_source": self.sections_source,
            "credit": self.credit,
            "course_type": self.course_type,
            "course_hour": self.course_hour,
            "exam_type": self.exam_type,
        }


@dataclass(frozen=True)
class TimetableWeek:
    term: TimetableTerm
    week: int
    dates: tuple[dt.date, ...]
    meetings: tuple[TimetableMeeting, ...]

    @property
    def empty(self) -> bool:
        return not self.meetings


def _obj(value: Any, what: str) -> dict:
    if not isinstance(value, dict):
        raise TimetableError("schema_changed", f"{what}不是对象。")
    return value


def _list(value: Any, what: str) -> list:
    if not isinstance(value, list):
        raise TimetableError("schema_changed", f"{what}不是数组。")
    return value


def _text(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text or None


def _required_text(value: Any, what: str) -> str:
    text = _text(value)
    if text is None:
        raise TimetableError("schema_changed", f"{what}缺失或为空。")
    return text


def _int(value: Any, what: str) -> int:
    if isinstance(value, bool) or value is None:
        raise TimetableError("schema_changed", f"{what}不是整数。")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
        return int(value.strip())
    raise TimetableError("schema_changed", f"{what}不是整数。")


def _date(value: Any, what: str) -> dt.date:
    text = _required_text(value, what)
    if not _DATE.fullmatch(text):
        raise TimetableError("schema_changed", f"{what}不是 YYYY-MM-DD 日期。")
    try:
        return dt.date.fromisoformat(text)
    except ValueError as exc:
        raise TimetableError("schema_changed", f"{what}不是有效日期。") from exc


def _normalize_weekday(value: Any) -> int | None:
    try:
        number = _int(value, "weekday")
    except TimetableError:
        return None
    if number == 0:
        return 7
    if 1 <= number <= 7:
        return number
    return None


def _week_numbers(value: Any, total_weeks: int) -> tuple[int, ...]:
    text = _required_text(value, "课程 week")
    parts = [part.strip() for part in text.split(",")]
    if not parts or any(not re.fullmatch(r"\d+", part) for part in parts):
        raise TimetableError("schema_changed", "课程 week 不是逗号分隔的整数周次列表。")
    numbers = tuple(int(part) for part in parts)
    if len(set(numbers)) != len(numbers):
        raise TimetableError("schema_changed", "课程 week 含有重复周次。")
    if any(not 1 <= number <= total_weeks for number in numbers):
        raise TimetableError("schema_changed", "课程 week 超出该学期总周数。")
    return numbers


def _consecutive(periods: Iterable[int]) -> list[tuple[int, int]]:
    ordered = sorted(set(periods))
    groups: list[tuple[int, int]] = []
    for period in ordered:
        if not groups or period != groups[-1][1] + 1:
            groups.append((period, period))
        else:
            groups[-1] = (groups[-1][0], period)
    return groups


def _check_dates(raw: Any, what: str) -> tuple[dt.date, ...]:
    values = _list(raw, what)
    if len(values) != 7:
        raise TimetableError("schema_changed", f"{what}必须是连续 7 天。")
    dates = tuple(_date(value, f"{what}[{index}]") for index, value in enumerate(values))
    if any(dates[index] + dt.timedelta(days=1) != dates[index + 1] for index in range(6)):
        raise TimetableError("schema_changed", f"{what}不是连续 7 天。")
    return dates


def parse_index(payload: Any) -> TimetableIndex:
    """Parse ``get-index``; 10013 is the only explicit auth-expired signal."""

    root = _obj(payload, "get-index 响应")
    code = _int(root.get("e"), "get-index.e")
    if code == 10013:
        raise TimetableError(
            "auth_required",
            "课表业务会话已失效，请重新完成本地正常登录。",
            action="complete_official_login",
        )
    if code != 0:
        raise TimetableError("upstream_error", f"学校课表接口返回业务错误（e={code}）。")

    data = _obj(root.get("d"), "get-index.d")
    raw_terms = _list(data.get("termInfo"), "get-index.d.termInfo")
    if not raw_terms:
        raise TimetableError("schema_changed", "学校未返回可用的学期列表。")

    terms: list[TimetableTerm] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(raw_terms):
        obj = _obj(item, f"termInfo[{index}]")
        year = _required_text(obj.get("year"), f"termInfo[{index}].year")
        term = _required_text(obj.get("term"), f"termInfo[{index}].term")
        startday = _date(obj.get("startday"), f"termInfo[{index}].startday")
        total_weeks = _int(obj.get("countweek"), f"termInfo[{index}].countweek")
        if not 1 <= total_weeks <= MAX_WEEKS:
            raise TimetableError("schema_changed", "学期总周数超出允许范围。")
        key = (year, term)
        if key in seen:
            raise TimetableError("schema_changed", "学期列表含有重复学年/学期。")
        seen.add(key)
        terms.append(
            TimetableTerm(
                year=year,
                term=term,
                startday=startday,
                total_weeks=total_weeks,
                id=_text(obj.get("id")),
                label=f"{year} {TERM_LABELS.get(term, '第' + term + '学期')}",
            )
        )

    params = data.get("params")
    params = params if isinstance(params, dict) else {}
    current_year = _text(params.get("year"))
    current_term = _text(params.get("term"))
    current_week: int | None = None
    if params.get("week") is not None:
        current_week = _int(params.get("week"), "params.week")
        if not 1 <= current_week <= MAX_WEEKS:
            raise TimetableError("schema_changed", "当前教学周超出允许范围。")
    current_weekdays: tuple[dt.date, ...] | None = None
    if data.get("weekdays") is not None:
        current_weekdays = _check_dates(data.get("weekdays"), "get-index.d.weekdays")
    current_weekday = (
        _normalize_weekday(data.get("weekday"))
        if data.get("weekday") is not None
        else None
    )

    return TimetableIndex(
        terms=tuple(terms),
        current_year=current_year,
        current_term=current_term,
        current_week=current_week,
        current_weekdays=current_weekdays,
        current_weekday=current_weekday,
    )


def parse_week(payload: Any, term: TimetableTerm, week: int) -> TimetableWeek:
    """Parse one week and reject any response that describes another week."""

    if not 1 <= week <= term.total_weeks:
        raise TimetableError("invalid_input", "week 超出该学期总周数。")
    root = _obj(payload, "get-datatmp 响应")
    code = _int(root.get("e"), "get-datatmp.e")
    if code == 10013:
        raise TimetableError(
            "auth_required",
            "课表业务会话已失效，请重新完成本地正常登录。",
            action="complete_official_login",
        )
    if code != 0:
        raise TimetableError("upstream_error", f"学校课表接口返回业务错误（e={code}）。")

    data = _obj(root.get("d"), "get-datatmp.d")
    classes = _list(data.get("classes"), "get-datatmp.d.classes")
    if len(classes) != 7:
        raise TimetableError("schema_changed", "课表 classes 必须恰好包含 7 天。")
    dates = _check_dates(data.get("weekdays"), "get-datatmp.d.weekdays")
    expected_start = term.startday + dt.timedelta(days=(week - 1) * 7)
    if dates[0] != expected_start:
        raise TimetableError("schema_changed", "返回的周日期与所选学期/教学周不一致。")

    meetings: list[TimetableMeeting] = []
    for day_index, day_value in enumerate(classes):
        day = _obj(day_value, f"classes[{day_index}]")
        date = dates[day_index]
        weekday = date.isoweekday()
        for key, entries_value in day.items():
            match = re.fullmatch(r"c_(\d{1,2})", str(key))
            if not match:
                raise TimetableError("schema_changed", "课表出现无法识别的节次键。")
            cell_start = int(match.group(1))
            if not 1 <= cell_start <= MAX_PERIOD:
                raise TimetableError("schema_changed", "课表节次键超出 1–14。")
            entries = _list(entries_value, "课表节次单元格")
            for entry_value in entries:
                entry = _obj(entry_value, "课程记录")
                name = _required_text(entry.get("course_name"), "课程 course_name")
                raw_periods = _list(entry.get("lessArr"), "课程 lessArr")
                if not raw_periods:
                    raise TimetableError("schema_changed", "课程 lessArr 为空。")
                periods = tuple(_int(value, "课程 lessArr 项") for value in raw_periods)
                if any(not 1 <= value <= MAX_PERIOD for value in periods):
                    raise TimetableError("schema_changed", "课程节次超出 1–14。")
                if len(set(periods)) != len(periods):
                    raise TimetableError("schema_changed", "课程 lessArr 含有重复节次。")
                if cell_start != min(periods):
                    raise TimetableError("schema_changed", "课程 c_N 与 lessArr 起始节次不一致。")

                week_label = _required_text(entry.get("week"), "课程 week")
                weeks = _week_numbers(week_label, term.total_weeks)
                if week not in weeks:
                    raise TimetableError("schema_changed", "课程 week 列表不包含请求的教学周。")

                declared_weekday = None
                if entry.get("weekday") is not None:
                    declared_weekday = _normalize_weekday(entry.get("weekday"))
                    if declared_weekday is None:
                        raise TimetableError("schema_changed", "课程 weekday 不是 0–7。")
                    if declared_weekday != weekday:
                        raise TimetableError("schema_changed", "课程 weekday 与周日期不一致。")

                total_length: int | None = None
                if entry.get("totalLength") is not None:
                    total_length = _int(entry.get("totalLength"), "课程 totalLength")
                    if total_length != len(periods):
                        raise TimetableError(
                            "schema_changed", "课程 totalLength 与 lessArr 不一致。"
                        )
                order: int | None = None
                if entry.get("order") is not None:
                    order = _int(entry.get("order"), "课程 order")
                    if order != min(periods):
                        raise TimetableError(
                            "schema_changed", "课程 order 与 lessArr 起始节次不一致。"
                        )

                groups = _consecutive(periods)
                split = len(groups) > 1
                for start, end in groups:
                    group = tuple(range(start, end + 1))
                    meetings.append(
                        TimetableMeeting(
                            course_name=name,
                            weekday=weekday,
                            start_period=start,
                            end_period=end,
                            periods=group,
                            source_periods=periods,
                            weeks=weeks,
                            week_label=week_label,
                            teacher=_text(entry.get("teacher")),
                            location=_text(entry.get("location")),
                            course_id=_text(entry.get("course_id")),
                            week_type=_text(entry.get("week_type")),
                            order=order,
                            total_length=total_length,
                            lessons=_text(entry.get("lessons")),
                            course_time=_text(entry.get("course_time")),
                            sections_source=(
                                "lessArr_split_noncontiguous" if split else "lessArr"
                            ),
                            credit=_text(entry.get("credit")),
                            course_type=_text(entry.get("course_type")),
                            course_hour=_text(entry.get("course_hour")),
                            exam_type=_text(entry.get("khfs")),
                        )
                    )

    return TimetableWeek(term=term, week=week, dates=dates, meetings=tuple(meetings))


def _cookie_header(cookies: Sequence[TimetableCookie], host: str, path: str) -> str:
    values = [
        cookie.header_value()
        for cookie in cookies
        if cookie.matches(host, path, secure=True)
    ]
    return "; ".join(values)


class TimetableClient:
    """Fixed-origin async client.  Only tests may inject a transport."""

    def __init__(
        self,
        cookies: Sequence[TimetableCookie],
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: httpx.Timeout | None = None,
        max_bytes: int = MAX_RESPONSE_BYTES,
    ):
        self.cookies = tuple(cookies)
        self.transport = transport
        self.timeout = timeout or DEFAULT_TIMEOUT
        self.max_bytes = max_bytes
        self._lock = asyncio.Lock()

    async def _request(self, method: str, url: str, data: dict | None = None) -> Any:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": BASE_URL + PAGE_PATH,
        }
        cookie = _cookie_header(self.cookies, "app.upc.edu.cn", "/")
        if cookie:
            headers["Cookie"] = cookie
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        async with self._lock:
            try:
                async with httpx.AsyncClient(
                    transport=self.transport,
                    timeout=self.timeout,
                    follow_redirects=False,
                    trust_env=False,
                ) as client:
                    async with client.stream(method, url, headers=headers, data=data) as response:
                        status = response.status_code
                        if status in {301, 302, 303}:
                            raise TimetableError(
                                "auth_required",
                                "课表接口要求重新登录。",
                                action="complete_official_login",
                            )
                        if status in {307, 308}:
                            raise TimetableError("upstream_error", "课表接口返回意外重定向。")
                        if status in {401}:
                            raise TimetableError(
                                "auth_required",
                                "课表接口拒绝当前会话（HTTP 401）。",
                                action="complete_official_login",
                            )
                        if status == 403:
                            raise TimetableError("upstream_error", "课表接口拒绝访问（HTTP 403）。")
                        if status != 200:
                            raise TimetableError("upstream_error", f"课表接口返回 HTTP {status}。")
                        content_type = response.headers.get("content-type", "").lower()
                        if "application/json" not in content_type:
                            raise TimetableError("schema_changed", "课表接口未返回 JSON。")
                        chunks: list[bytes] = []
                        total = 0
                        async for chunk in response.aiter_bytes():
                            total += len(chunk)
                            if total > self.max_bytes:
                                raise TimetableError(
                                    "response_too_large",
                                    "课表接口响应超过本地大小上限。",
                                )
                            chunks.append(chunk)
            except TimetableError:
                raise
            except httpx.TimeoutException:
                raise TimetableError("timeout", "课表接口请求超时。") from None
            except httpx.ConnectError as exc:
                text = str(exc).lower()
                if "ssl" in text or "certificate" in text or "tls" in text:
                    raise TimetableError("tls_error", "课表接口 TLS 连接失败；未降级。") from None
                raise TimetableError("network_error", "无法连接课表接口。") from None
            except httpx.HTTPError:
                raise TimetableError("network_error", "课表接口传输失败。") from None
        try:
            return json.loads(b"".join(chunks))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise TimetableError("schema_changed", "课表接口响应不是有效 JSON。") from None

    async def get_index(self) -> TimetableIndex:
        return parse_index(await self._request("GET", BASE_URL + INDEX_PATH))

    async def get_week(self, term: TimetableTerm, week: int) -> TimetableWeek:
        payload = await self._request(
            "POST",
            BASE_URL + WEEK_PATH,
            data={
                "year": term.year,
                "term": term.term,
                "week": str(week),
                "type": WEEK_TYPE,
            },
        )
        return parse_week(payload, term, week)
