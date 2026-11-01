"""Strict, fixed-host client and parsers for e站通 (service.upc.edu.cn).

Only the six observed read-only endpoints are exposed. Callers cannot supply a
URL, host, header, cookie, account, HTTP method or arbitrary query. Upstream
free text is data, never an instruction; personal fields are projected before
crossing the tool boundary.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx

SERVICE_HOST = "service.upc.edu.cn"
BASE_URL = "https://service.upc.edu.cn"
SEARCH_PATH = "/api/apps/service-list"
CATALOG_PATH = "/api/apps/list"
DETAIL_PATH = "/api/apps/detail"
TODO_PATH = "/site/my-task/todo"
APPLICATIONS_PATH = "/site/process/inst-list"
COUNT_PATH = "/site/message/count"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_GUIDE_CHARS = 8_000
MAX_ITEM_TEXT = 300
MAX_PAGE = 50
MAX_PAGE_SIZE = 50
DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=15.0, pool=5.0)
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36 Edg/156.0.0.0"
)
JSON_HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": BASE_URL + "/v2/site/index",
}
_SAFE_QUERY_KEYS = {"app_id"}
_SENSITIVE_QUERY_KEYS = {
    "token",
    "ticket",
    "session",
    "sessionid",
    "jsessionid",
    "auth",
    "code",
    "state",
    "sig",
    "signature",
    "cookie",
}


class ServicePortalError(Exception):
    """Fixed, credential-free error safe for the CLI/MCP boundary."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        action: str | None = None,
        details: dict[str, Any] | None = None,
    ):
        self.code = code
        self.action = action
        self.details = details or {}
        super().__init__(message)

    def payload(self) -> dict:
        safe_messages = {
            "auth_required": "e站通需要本人登录；请先完成统一认证。",
            "auth_expired": "e站通会话已失效；请重试自动认证。",
            "credential_changed": "统一凭据已更换或清除；本次结果未采用。",
            "identity_mismatch": "现有 e站通会话与统一凭据不一致；请先清除该会话。",
            "user_action_required": "e站通要求二次验证；请完成官方交互后重试。",
            "auth_in_progress": "另一次 e站通认证正在进行；本次未重复认证。",
            "access_denied": "e站通拒绝访问当前事项或账号。",
            "service_timeout": "e站通请求超时。",
            "service_network_error": "e站通网络请求失败。",
            "service_upstream_error": "e站通服务暂时不可用。",
            "schema_changed": "e站通响应结构不符合已验证契约。",
            "unexpected_redirect": "e站通返回了非预期跳转。",
            "invalid_input": "e站通请求参数无效。",
            "upstream_error": "e站通返回了未识别的业务状态。",
            "unsupported": "本机不支持 e站通会话存储。",
            "local_error": "本地 e站通服务异常。",
        }
        error: dict[str, Any] = {
            "message": safe_messages.get(
                self.code, "e站通请求失败；未输出上游原始内容。"
            )
        }
        if self.action:
            error["action"] = self.action
        for key in ("kind", "retry_after_seconds", "upstream_code"):
            if key in self.details:
                error[key] = self.details[key]
        return {
            "status": self.code,
            "source_system": "service_portal",
            "session_ref": "self",
            "error": error,
        }


def now() -> str:
    from datetime import datetime

    return datetime.now().astimezone().isoformat(timespec="seconds")


def _int(value: Any, *, field: str, minimum: int | None = None) -> int:
    if type(value) is not int:
        raise ServicePortalError("schema_changed", f"e站通响应字段 {field} 不是整数。")
    if minimum is not None and value < minimum:
        raise ServicePortalError("schema_changed", f"e站通响应字段 {field} 超出范围。")
    return value


def _dict(value: Any, *, field: str) -> dict:
    if not isinstance(value, dict):
        raise ServicePortalError("schema_changed", f"e站通响应字段 {field} 不是对象。")
    return value


def _list(value: Any, *, field: str) -> list:
    if not isinstance(value, list):
        raise ServicePortalError("schema_changed", f"e站通响应字段 {field} 不是数组。")
    return value


def _text(value: Any, *, limit: int = MAX_ITEM_TEXT) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return None
    return cleaned[:limit]


def _first_text(*values: Any, limit: int = MAX_ITEM_TEXT) -> str | None:
    for value in values:
        text = _text(value, limit=limit)
        if text:
            return text
    return None


def _boolish(value: Any) -> bool:
    return bool(value) if isinstance(value, (bool, int)) else False


def parse_envelope(raw: bytes) -> dict:
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ServicePortalError("schema_changed", "e站通返回了非 JSON 响应。") from None
    if not isinstance(body, dict) or "e" not in body:
        raise ServicePortalError("schema_changed", "e站通响应缺少业务信封。")
    raw_code = body.get("e")
    try:
        code = int(raw_code)
    except (TypeError, ValueError):
        raise ServicePortalError("schema_changed", "e站通业务码格式不受支持。") from None
    if code == 10042:
        raise ServicePortalError(
            "auth_required", "e站通需要本人登录。", action="service_portal_auto_auth"
        )
    if code == 100421:
        raise ServicePortalError(
            "auth_expired", "e站通会话已失效。", action="service_portal_auto_auth"
        )
    if code == 30000:
        raise ServicePortalError(
            "user_action_required",
            "e站通要求二次验证；请完成官方交互后重试。",
            action="service_portal_unlock",
            details={"kind": "second_factor"},
        )
    if code != 0:
        raise ServicePortalError(
            "upstream_error",
            "e站通返回了未识别的业务状态。",
            details={"upstream_code": code},
        )
    return body


def access_kind(
    type_: Any,
    jump_type: Any,
    details_page: Any,
    url: str | None,
    *,
    is_folder: Any = 0,
    has_form: bool = False,
) -> str:
    if _boolish(is_folder):
        return "folder"
    if type_ == 4 and jump_type == 2:
        return "miniprogram"
    if type_ == 4 and jump_type == 1 and url:
        return "external_url"
    if type_ in (1, 2) and has_form:
        return "native_form"
    if type_ == 1:
        return "native_form"
    if type_ == 2:
        return "form_unknown"
    if type_ is None:
        return "unknown"
    return "guide_only"


def _url_host(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return None
    if parts.username or parts.password:
        return None
    return parts.hostname.lower()


def sanitize_external_url(value: Any) -> tuple[str | None, str | None, bool]:
    """Return (url, host, query_omitted); only http(s), no userinfo/fragment."""
    if not isinstance(value, str) or not value:
        return None, None, False
    try:
        parts = urlsplit(value)
    except ValueError:
        return None, None, False
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username
        or parts.password
    ):
        return None, None, False
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    omitted = False
    kept: list[tuple[str, str]] = []
    for key, val in pairs:
        if key.lower() in _SENSITIVE_QUERY_KEYS:
            omitted = True
            continue
        if key.lower() not in _SAFE_QUERY_KEYS:
            omitted = True
            continue
        if len(val) > 128 or any(ord(ch) < 32 for ch in val):
            omitted = True
            continue
        kept.append((key, val))
    query = urlencode(kept)
    try:
        port = parts.port
    except ValueError:
        return None, None, False
    host = f"{parts.hostname}:{port}" if port else parts.hostname
    clean = f"{parts.scheme}://{host}{parts.path}"
    if query:
        clean += "?" + query
    return clean, parts.hostname.lower(), omitted


class _TextExtractor(HTMLParser):
    _BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table"}
    _SKIP = {"script", "style", "noscript", "iframe", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag in self._SKIP:
            self.skip_depth += 1
        elif self.skip_depth == 0:
            if tag == "li":
                self.parts.append("\n- ")
            elif tag in self._BLOCK:
                self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self.skip_depth:
            self.skip_depth -= 1
        elif self.skip_depth == 0 and tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.skip_depth == 0:
            self.parts.append(data)


def html_to_text(value: Any, *, limit: int = MAX_GUIDE_CHARS) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    parser = _TextExtractor()
    try:
        parser.feed(value)
        parser.close()
    except Exception:
        return None
    text = unescape("".join(parser.parts)).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    text = "\n".join(line for line in lines if line).strip()
    if not text:
        return None
    return text[:limit]


def opaque_ref(account_hash: str | None, kind: str, *parts: Any) -> str | None:
    if not account_hash:
        return None
    if not any(part is not None for part in parts):
        return None
    seed = "\0".join(
        ["service-portal", account_hash, kind, *[str(part) for part in parts if part is not None]]
    )
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]


def _search_item(item: Any) -> dict:
    row = _dict(item, field="data[]")
    service_id = _int(row.get("id"), field="id", minimum=1)
    name = _text(row.get("name"), limit=200)
    if not name:
        raise ServicePortalError("schema_changed", "e站通事项缺少名称。")
    url = row.get("url_pc")
    if not isinstance(url, str) or not url:
        url = row.get("url")
    if not isinstance(url, str) or not url:
        url = None
    type_ = row.get("type") if type(row.get("type")) is int else None
    jump_type = row.get("jump_type") if type(row.get("jump_type")) is int else None
    details_page = row.get("details_page") if type(row.get("details_page")) is int else None
    return {
        "id": service_id,
        "name": name,
        "department": _text(row.get("departName"), limit=200),
        "type": type_,
        "access": access_kind(
            type_,
            jump_type,
            details_page,
            url,
            is_folder=row.get("is_folder"),
        ),
        "external_host": _url_host(url),
        "folder": _boolish(row.get("is_folder")),
        "id_ambiguous": False,
    }


def parse_catalog(body: dict) -> dict:
    """Parse the complete flat ``/api/apps/list`` snapshot.

    The observed authenticated catalog is flat but is not a clean service-only
    table: it contains folder rows whose ids overlap service ids, and a folder
    id can therefore repeat.  Rows are preserved in source order; duplicate ids
    are reported rather than collapsed, because de-duplicating by id would drop
    a real service.  Folder rows are navigation containers and are excluded
    from the service search result, but they remain visible in these counts.
    """

    d = _dict(body.get("d"), field="d")
    raw = _list(d.get("list"), field="d.list")
    items = [_search_item(item) for item in raw]
    id_counts = Counter(item["id"] for item in items)
    folders = [item for item in items if item["folder"]]
    services = [item for item in items if not item["folder"]]
    service_id_counts = Counter(item["id"] for item in services)
    duplicate_ids = sorted(item_id for item_id, count in id_counts.items() if count > 1)
    duplicate_service_ids = sorted(
        item_id for item_id, count in service_id_counts.items() if count > 1
    )
    folder_service_ids = {item["id"] for item in folders}
    service_ids = {item["id"] for item in services}
    for item in items:
        item["id_ambiguous"] = id_counts[item["id"]] > 1
    return {
        "total": len(items),
        "items": items,
        "complete": True,
        "source": "apps/list",
        "folder_count": len(folders),
        "searchable_total": len(services),
        "duplicate_id_count": len(duplicate_ids),
        "duplicate_row_count": sum(
            1 for item in items if id_counts[item["id"]] > 1
        ),
        "duplicate_service_id_count": len(duplicate_service_ids),
        "duplicate_service_ids": duplicate_service_ids[:50],
        "folder_service_id_collision_count": len(folder_service_ids & service_ids),
    }


def parse_search(body: dict, *, page: int, page_size: int) -> dict:
    d = _dict(body.get("d"), field="d")
    data = _list(d.get("data"), field="data")
    total = _int(d.get("total"), field="total", minimum=0)
    if total < len(data):
        raise ServicePortalError("schema_changed", "e站通分页总数小于本页条数。")
    items = [_search_item(item) for item in data]
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "count": len(items),
        "has_more": total > page * page_size,
        "items": items,
    }


def _material_list(value: Any) -> tuple[list[dict] | None, str]:
    if value is None:
        return None, "missing"
    if not isinstance(value, list):
        raise ServicePortalError("schema_changed", "e站通办理材料字段不是数组。")
    if not value:
        return [], "empty"
    out: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            raise ServicePortalError("schema_changed", "e站通办理材料条目不是对象。")
        field = _text(item.get("field"), limit=80)
        if field in {"person", "person_tel"}:
            continue
        label = _text(item.get("label"), limit=120) or field
        content = html_to_text(item.get("value"), limit=1000)
        if not label and not content:
            continue
        out.append({"label": label, "value": content})
        if len(out) >= 40:
            break
    return out, "available" if out else "empty"


def parse_guide(body: dict, service_id: int, *, department: str | None) -> dict:
    d = _dict(body.get("d"), field="d")
    if d.get("id") != service_id:
        raise ServicePortalError("schema_changed", "e站通指南返回了不同的事项。")
    name = _text(d.get("name"), limit=200)
    if not name:
        raise ServicePortalError("schema_changed", "e站通指南缺少事项名称。")
    explain = d.get("explain")
    description = html_to_text(explain)
    materials, materials_status = _material_list(d.get("detailed_information"))
    forms = d.get("forms")
    form_count = len(forms) if isinstance(forms, list) else None
    start_nodes = []
    raw_start = d.get("start_node")
    if isinstance(raw_start, list):
        for node in raw_start[:5]:
            if isinstance(node, dict):
                text = _text(node.get("node_name"), limit=80)
                if text:
                    start_nodes.append(text)
    raw_external = d.get("url_pc")
    if not isinstance(raw_external, str) or not raw_external:
        raw_external = d.get("url")
    external_url, external_host, query_omitted = sanitize_external_url(raw_external)
    type_ = d.get("type") if type(d.get("type")) is int else None
    jump_type = d.get("jump_type") if type(d.get("jump_type")) is int else None
    details_page = d.get("details_page") if type(d.get("details_page")) is int else None
    warnings: list[str] = []
    if materials_status == "missing":
        warnings.append("structured_materials_missing")
    if query_omitted:
        warnings.append("external_url_query_omitted")
    if description and len(explain or "") > MAX_GUIDE_CHARS:
        warnings.append("description_truncated")
    return {
        "service_id": service_id,
        "name": name,
        "department": department,
        "type": type_,
        "access": access_kind(
            type_, jump_type, details_page, external_url, has_form=bool(form_count)
        ),
        "is_apply": d.get("is_apply") if isinstance(d.get("is_apply"), bool) else None,
        "apply_state": "unknown",
        "apply_state_known": False,
        "apply_state_basis": "guide_does_not_expose_application_state",
        "description": description,
        "description_length": len(description) if description else 0,
        "description_truncated": bool(
            isinstance(explain, str) and len(explain) > MAX_GUIDE_CHARS
        ),
        "materials": materials,
        "materials_status": materials_status,
        "handle_place": _text(d.get("handle_place"), limit=200),
        "handling_times": _text(d.get("handling_times"), limit=200),
        "is_charge": _text(d.get("is_charge"), limit=80),
        "charge": _text(d.get("charge"), limit=120),
        "form_count": form_count,
        "start_nodes": start_nodes,
        "external_url": external_url,
        "external_host": external_host,
        "external_url_query_omitted": query_omitted,
        "warnings": warnings,
    }


def project_todo(item: Any, account_hash: str | None) -> dict:
    row = _dict(item, field="list[]")
    title = _first_text(row.get("task_name"), row.get("app_name"), row.get("tag_name"))
    status = row.get("task_status")
    if type(status) is not int and not isinstance(status, str):
        status = row.get("status") if type(row.get("status")) is int else None
    created = _first_text(row.get("created"), row.get("send_time"), limit=40)
    return {
        "ref": opaque_ref(
            account_hash, "todo", row.get("task_id"), row.get("inst_id"), row.get("id")
        ),
        "title": title,
        "status": status,
        "current_node": _text(row.get("node_name"), limit=80),
        "created_at": created,
        "third_party": bool(row.get("thirdparty_id")),
    }


def project_application(item: Any, account_hash: str | None) -> dict:
    row = _dict(item, field="list[]")
    title = _first_text(
        row.get("thirdparty_name"), row.get("app_name"), row.get("task_name"), limit=200
    )
    title_source = (
        "thirdparty_name"
        if _text(row.get("thirdparty_name"), limit=200)
        else "app_name"
        if _text(row.get("app_name"), limit=200)
        else None
    )
    nodes = row.get("nodes")
    if nodes is not None and not isinstance(nodes, list):
        raise ServicePortalError("schema_changed", "e站通申请流程节点不是数组。")
    percent = row.get("percent")
    if percent is not None and type(percent) is not int:
        raise ServicePortalError("schema_changed", "e站通申请进度不是整数。")
    node_count = len(nodes) if isinstance(nodes, list) and nodes else None
    progress = (
        {"percent": percent, "node_count": node_count}
        if node_count is not None and percent is not None
        else None
    )
    return {
        "ref": opaque_ref(
            account_hash, "application", row.get("inst_id"), row.get("id")
        ),
        "title": title,
        "title_source": title_source,
        "status": _text(row.get("inst_status"), limit=80),
        "status_code": row.get("status") if type(row.get("status")) is int else None,
        "third_party": bool(row.get("thirdparty_id")),
        "created_at": _text(row.get("created"), limit=40),
        "progress": progress,
        "progress_known": progress is not None,
        "flow_node_count": node_count,
    }


def _page_contract(
    *, page: int, page_size: int, total: int, count: int
) -> dict:
    """Describe one observed page without claiming other pages were read."""

    pages_total = max(1, (total + page_size - 1) // page_size)
    offset = (page - 1) * page_size
    page_in_range = page <= pages_total
    expected = min(page_size, max(0, total - offset))
    has_more = offset + count < total
    return {
        "pages_total": pages_total,
        "page_in_range": page_in_range,
        "complete_for_page": page_in_range and count == expected,
        "has_more": has_more,
        "next_page": page + 1 if has_more else None,
        "remaining_after_page": max(0, total - offset - count),
    }


def parse_todo(body: dict, account_hash: str | None, *, page: int, page_size: int) -> dict:
    d = _dict(body.get("d"), field="d")
    items = _list(d.get("list"), field="list")
    total = _int(d.get("total"), field="total", minimum=0)
    if len(items) > page_size:
        raise ServicePortalError("schema_changed", "e站通待办单页条数超过请求上限。")
    offset = (page - 1) * page_size
    if items and offset + len(items) > total:
        raise ServicePortalError("schema_changed", "e站通待办分页与总数不一致。")
    projected = [project_todo(item, account_hash) for item in items]
    pagination = _page_contract(
        page=page, page_size=page_size, total=total, count=len(projected)
    )
    if total == 0:
        complete_for_scope, scope_reason = True, "empty_scope"
    elif page == 1 and len(projected) == total:
        complete_for_scope, scope_reason = True, "single_page_covers_all"
    else:
        complete_for_scope, scope_reason = False, "other_pages_not_included"
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "count": len(projected),
        "returned": len(projected),
        "empty": total == 0,
        "empty_page": not projected,
        "complete_for_scope": complete_for_scope,
        "complete_for_scope_reason": scope_reason,
        **pagination,
        "items": projected,
        "status_filter": 1,
        "status_filter_source": "verified_request_filter",
    }


def parse_applications(
    body: dict, account_hash: str | None, *, page: int, page_size: int
) -> dict:
    d = _dict(body.get("d"), field="d")
    items = _list(d.get("list"), field="list")
    total = _int(d.get("total"), field="total", minimum=0)
    if len(items) > page_size:
        raise ServicePortalError("schema_changed", "e站通申请单页条数超过请求上限。")
    offset = (page - 1) * page_size
    if items and offset + len(items) > total:
        raise ServicePortalError("schema_changed", "e站通申请分页与总数不一致。")
    projected = [project_application(item, account_hash) for item in items]
    pagination = _page_contract(
        page=page, page_size=page_size, total=total, count=len(projected)
    )
    if total == 0:
        complete_for_scope, scope_reason = True, "empty_scope"
    elif page == 1 and len(projected) == total:
        complete_for_scope, scope_reason = True, "single_page_covers_all"
    else:
        complete_for_scope, scope_reason = False, "other_pages_not_included"
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "count": len(projected),
        "returned": len(projected),
        "empty": total == 0,
        "empty_page": not projected,
        "complete_for_scope": complete_for_scope,
        "complete_for_scope_reason": scope_reason,
        **pagination,
        "items": projected,
        "third_party_count": sum(1 for item in projected if item["third_party"]),
        "status_filter": 1,
        "status_filter_source": "verified_request_filter",
    }


class ServicePortalClient:
    def __init__(
        self,
        cookies: httpx.Cookies | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: httpx.Timeout | None = None,
    ):
        kwargs: dict[str, Any] = {
            "base_url": BASE_URL,
            "cookies": cookies or httpx.Cookies(),
            "follow_redirects": False,
            "timeout": timeout or DEFAULT_TIMEOUT,
            "trust_env": False,
            "headers": JSON_HEADERS,
        }
        if transport is not None:
            kwargs["transport"] = transport
        self._http = httpx.Client(**kwargs)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> ServicePortalClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _request(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        personal: bool = False,
    ) -> dict:
        try:
            with self._http.stream("GET", path, params=params) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = str(response.headers.get("location", ""))
                    try:
                        target = urlsplit(location)
                    except ValueError:
                        raise ServicePortalError(
                            "unexpected_redirect", "e站通返回了非预期跳转。"
                        ) from None
                    if personal and (
                        target.hostname == "cas.upc.edu.cn"
                        or target.path.startswith("/cas/")
                        or target.path.startswith("/site/login")
                    ):
                        raise ServicePortalError(
                            "auth_required",
                            "e站通要求本人登录。",
                            action="service_portal_auto_auth",
                        )
                    raise ServicePortalError("unexpected_redirect", "e站通返回了非预期跳转。")
                if response.status_code == 403:
                    raise ServicePortalError("access_denied", "e站通拒绝访问。")
                if response.status_code == 401:
                    raise ServicePortalError(
                        "auth_required",
                        "e站通拒绝了当前会话。",
                        action="service_portal_auto_auth",
                    )
                if response.status_code >= 500:
                    raise ServicePortalError(
                        "service_upstream_error", "e站通服务暂时不可用。"
                    )
                if response.status_code != 200:
                    raise ServicePortalError(
                        "service_upstream_error", "e站通返回了非预期状态。"
                    )
                content_type = response.headers.get("content-type", "")
                if "application/json" not in content_type.lower():
                    raise ServicePortalError("schema_changed", "e站通返回了非 JSON 响应。")
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise ServicePortalError(
                            "schema_changed", "e站通响应超过本地大小限制。"
                        )
                    chunks.append(chunk)
                raw = b"".join(chunks)
        except ServicePortalError:
            raise
        except httpx.TimeoutException:
            raise ServicePortalError("service_timeout", "e站通请求超时。") from None
        except httpx.HTTPError:
            raise ServicePortalError("service_network_error", "e站通网络请求失败。") from None
        return parse_envelope(raw)

    def search(self, *, keyword: str | None, page: int, page_size: int) -> dict:
        params: dict[str, Any] = {"p": page, "page_size": page_size}
        if keyword:
            params["keyword"] = keyword
        body = self._request(SEARCH_PATH, params=params)
        return parse_search(body, page=page, page_size=page_size)

    def catalog(self) -> dict:
        body = self._request(CATALOG_PATH)
        return parse_catalog(body)

    def catalog_search(
        self, *, keyword: str | None, page: int, page_size: int
    ) -> dict:
        catalog = self.catalog()
        needle = (keyword or "").strip().casefold()
        terms = [term for term in needle.split() if term]
        # Folder rows are navigation containers whose ids collide with real
        # services in the observed snapshot.  They are counted and reported by
        # the catalog metadata but are not returned as selectable services.
        searchable = [item for item in catalog["items"] if not item["folder"]]

        def matches(item: dict) -> bool:
            if not terms:
                return True
            haystack = " ".join(
                value
                for value in (item.get("name"), item.get("department"))
                if value
            ).casefold()
            return all(term in haystack for term in terms)

        matched = [item for item in searchable if matches(item)]
        start = (page - 1) * page_size
        items = matched[start : start + page_size]
        pagination = _page_contract(
            page=page, page_size=page_size, total=len(matched), count=len(items)
        )
        warnings: list[str] = []
        if catalog["folder_count"]:
            warnings.append("catalog_folders_excluded")
        if catalog["duplicate_service_id_count"]:
            warnings.append("catalog_duplicate_service_ids")
        if catalog["folder_service_id_collision_count"]:
            warnings.append("catalog_folder_id_collisions")
        catalog_meta = {
            key: value
            for key, value in catalog.items()
            if key != "items"
        }
        catalog_meta["matched_total"] = len(matched)
        catalog_meta["retrieved_via"] = CATALOG_PATH
        return {
            "total": len(matched),
            "page": page,
            "page_size": page_size,
            "count": len(items),
            "returned": len(items),
            "empty": not matched,
            "empty_page": not items,
            "complete_for_scope": catalog["complete"] is True,
            "complete_for_scope_reason": "full_catalog_fetched",
            **pagination,
            "items": items,
            "catalog": catalog_meta,
            "warnings": warnings,
        }

    def guide(self, service_id: int, *, department: str | None) -> dict:
        body = self._request(DETAIL_PATH, params={"id": service_id, "userview": 1})
        return parse_guide(body, service_id, department=department)

    def todos(
        self, account_hash: str | None, *, page: int = 1, page_size: int = 20
    ) -> dict:
        body = self._request(
            TODO_PATH,
            params={"p": page, "page_size": page_size, "status": 1},
            personal=True,
        )
        return parse_todo(body, account_hash, page=page, page_size=page_size)

    def applications(
        self, account_hash: str | None, *, page: int = 1, page_size: int = 20
    ) -> dict:
        body = self._request(
            APPLICATIONS_PATH,
            params={"p": page, "page_size": page_size, "status": 1},
            personal=True,
        )
        return parse_applications(
            body, account_hash, page=page, page_size=page_size
        )

    def verify(self) -> dict:
        body = self._request(COUNT_PATH, personal=True)
        d = _dict(body.get("d"), field="d")
        num = d.get("num")
        if type(num) is not int or num < 0:
            raise ServicePortalError("schema_changed", "e站通未读数字段不是非负整数。")
        return {"unread_count": num}
