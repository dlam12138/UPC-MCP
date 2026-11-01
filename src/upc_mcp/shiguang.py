"""Shiguang adapter: fixed read-only queries plus one explicit registration write.

The adapter accepts an already-authenticated ``httpx.Client`` and only calls
fixed, observed endpoints. It never accepts a URL, header, cookie, account or
password from the model.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx

BASE = "https://dekt.upc.edu.cn"
LIST_PATH = "/actvMng/actvMngFront!jumpActvCampListPageForWeb.action"
MY_JOIN_PATH = "/actvMng/actvMngFront!jumpActvMyJoinListPageForWeb.action"
DETAIL_PATHS = {
    "activity": "/actvMng/actvMngFront!jumpActvCampViewPageForWeb.action",
    "social_practice": "/actvMng/actvMngFront!jumpSocialPracticeViewPageForWeb.action",
}
REGISTER_PATHS = {
    "activity": "/actvMngAjax/actvMngFront!goEnrollInForWeb.action",
    "social_practice": "/actvMngAjax/actvMngFront!goEnrollInSocialForWeb.action",
}
REGISTER_BY_CODE_PATH = "/actvMngAjax/actvMngFront!goEnrollInByCodeForWeb.action"
REPORT_CARD_PATH = "/reportCardMng/reportCardMngFront!jumpReportCardFirstForWeb.action"
REPORT_CARD_AJAX_PATH = (
    "/reportCardMngAjax/reportCardMngFront!querySelectedHour.action"
)
ONLINE_TYPES = {"001", "002", "003", "004"}
BASE_TYPES = {"001", "002", "003", "004", "005"}
MODULES = (
    (1, "思想成长类", ("001", "006")),
    (2, "创新创业类", ("002", "007")),
    (3, "文体发展类", ("003", "008")),
    (4, "社会实践与志愿服务类", ("004", "009")),
    (5, "工作履历与技能培训类", ("005", "010")),
)
MODULE_BY_CODE = {
    code: (module_id, label)
    for module_id, label, codes in MODULES
    for code in codes
}
MAX_HTML_BYTES = 3_000_000
MAX_JSON_BYTES = 200_000
MAX_KEYWORD = 40
DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}
BLOCK_TAGS = {
    "div", "p", "li", "tr", "br", "h1", "h2", "h3", "h4", "section", "article",
}


class ShiguangError(Exception):
    """Fixed, credential-free error crossing the adapter/service boundary."""

    def __init__(self, code: str, message: str, details: dict | None = None):
        self.code = code
        self.details = details or {}
        super().__init__(message)

    def payload(self) -> dict:
        payload = {
            "status": self.code,
            "source_system": "shiguang",
            **self.details,
            "error": {"message": str(self)},
        }
        return payload


@dataclass(eq=False)
class Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[Node | str] = field(default_factory=list)
    parent: Node | None = None

    def classes(self) -> set[str]:
        return set((self.attrs.get("class") or "").split())

    def text(self) -> str:
        chunks: list[str] = []

        def walk(node: Node | str) -> None:
            if isinstance(node, str):
                chunks.append(node)
                return
            for child in node.children:
                walk(child)
            if node.tag in BLOCK_TAGS:
                chunks.append("\n")

        walk(self)
        value = "".join(chunks)
        value = re.sub(r"[ \t\r\f\v]+", " ", value)
        value = re.sub(r"\n\s*\n+", "\n", value)
        return value.strip()

    def find(self, tag: str | None = None, cls: str | None = None) -> Node | None:
        for node in self.walk():
            if tag and node.tag != tag:
                continue
            if cls and cls not in node.classes():
                continue
            return node
        return None

    def find_all(self, tag: str | None = None, cls: str | None = None) -> list[Node]:
        result = []
        for node in self.walk():
            if tag and node.tag != tag:
                continue
            if cls and cls not in node.classes():
                continue
            result.append(node)
        return result

    def walk(self) -> Iterable[Node]:
        for child in self.children:
            if isinstance(child, Node):
                yield child
                yield from child.walk()

    def ancestor_with(self, cls: str) -> Node | None:
        current = self.parent
        while current is not None:
            if cls in current.classes():
                return current
            current = current.parent
        return None


class _TreeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#document")
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag, {key: value or "" for key, value in attrs}, parent=self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag, {key: value or "" for key, value in attrs}, parent=self.stack[-1])
        self.stack[-1].children.append(node)

    def handle_data(self, data: str) -> None:
        if data:
            self.stack[-1].children.append(data)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return


def parse_html(text: str) -> Node:
    parser = _TreeParser()
    parser.feed(text)
    return parser.root


def _field(text: str, label: str) -> str | None:
    match = re.search(rf"{re.escape(label)}\s*[：:]\s*(.+)", text)
    return re.sub(r"\s+", " ", match.group(1)).strip() if match else None


def _activity_id(href: str) -> str | None:
    values = parse_qs(urlsplit(href).query).get("actvModel.activityId")
    return values[0] if values else None


def _kind_from_href(href: str) -> str:
    return "social_practice" if "jumpSocialPracticeViewPageForWeb" in href else "activity"


def _parse_card(card: Node) -> dict | None:
    title_link = card.find("a", "ellipsis") or card.find("a", "more")
    if title_link is None:
        return None
    href = title_link.attrs.get("href") or ""
    if not href:
        return None
    detail_link = card.find("a", "more") or title_link
    detail_href = detail_link.attrs.get("href") or href
    text = card.text()
    image = card.find("img")
    image_src = image.attrs.get("src") if image else None
    return {
        "title": title_link.text(),
        "activity_id": _activity_id(detail_href) or _activity_id(href),
        "kind": _kind_from_href(detail_href),
        "detail_path": urlsplit(detail_href).path,
        "image_url": urljoin(BASE + "/", image_src) if image_src else None,
        "signup_deadline": _field(text, "报名截止时间") or _field(text, "报名时间"),
        "activity_time": _field(text, "活动时间"),
        "category": _field(text, "分类"),
        "location": _field(text, "地点"),
        "hours": _field(text, "学时"),
        "credits": _field(text, "学分"),
    }


def parse_activity_list(text: str, page: int) -> dict:
    root = parse_html(text)
    container = root.find("div", "active_2_list")
    if container is None:
        raise ShiguangError("parse_error", "活动列表结构不符合预期。")
    links = container.find_all("a", "ellipsis")
    if not links:
        # A genuinely empty list has the container but no card-like nodes.
        if container.find_all("div", "li") or container.find_all("li"):
            raise ShiguangError("parse_error", "活动列表存在卡片但无法解析。")
        return {"items": [], "total_pages": _total_pages(text), "next_page": None}

    cards: list[Node] = []
    for link in links:
        card = link.ancestor_with("li")
        if card is not None and card not in cards:
            cards.append(card)
    items = [item for card in cards if (item := _parse_card(card))]
    if not cards or len(items) != len(cards):
        raise ShiguangError("parse_error", "活动卡片结构已变化，未返回不完整数据。")
    total_pages = _total_pages(text)
    return {
        "items": items,
        "total_pages": total_pages,
        "next_page": page + 1 if total_pages and page < total_pages else None,
    }


def _total_pages(text: str) -> int | None:
    match = re.search(r"共(?:&nbsp;|\s)*(\d+)(?:&nbsp;|\s)*页", text)
    return int(match.group(1)) if match else None


def parse_detail(text: str) -> dict:
    root = parse_html(text)
    left = root.find("div", "activeD_l")
    if left is None:
        raise ShiguangError("parse_error", "活动详情结构不符合预期。")
    title_node = left.find("div", "t2")
    description_node = left.find("div", "djs")
    fields: dict[str, str] = {}
    labels = {
        "归属组织": "organizer",
        "活动分类": "category",
        "活动标签": "tags",
        "组织形式": "format",
        "培养目标": "goal",
        "能力指标": "ability",
        "活动地点": "location",
        "报名时间": "signup_time",
        "活动时间": "activity_time",
        "活动院系": "colleges",
        "活动年级": "grades",
        "学时": "hours",
        "学分": "credits",
    }
    for item in left.find_all("div", "li"):
        value = item.text()
        match = re.match(r"^([^：:]{1,24})[：:]\s*(.+)$", value, re.S)
        if not match:
            continue
        label, content = match.group(1).strip(), re.sub(r"\s+", " ", match.group(2)).strip()
        # Contact person/phone are deliberately omitted from the returned contract.
        if label.replace(" ", "") in {"联系人", "联系电话"}:
            continue
        key = labels.get(label)
        if key:
            fields[key] = content
    return {
        "title": title_node.text() if title_node else None,
        "description": description_node.text() if description_node else None,
        "fields": fields,
    }


def _looks_like_login(text: str) -> bool:
    head = text[:30000]
    lowered = head.lower()
    return ('id="loginform"' in lowered or 'name="execution"' in lowered) and (
        "cas/login" in lowered or "统一身份认证" in head[:5000]
    )


def _decimal_text(value: str | None, label: str) -> str | None:
    if value is None or not value.strip():
        return None
    raw = value.strip()
    try:
        number = Decimal(raw)
    except InvalidOperation:
        raise ShiguangError("parse_error", f"{label} 不是可解析的数值。") from None
    if not number.is_finite():
        raise ShiguangError("parse_error", f"{label} 不是有限数值。")
    return raw


def _hidden_inputs(root: Node) -> dict[str, str]:
    return {
        node.attrs.get("id", ""): node.attrs.get("value", "")
        for node in root.find_all("input")
        if node.attrs.get("id")
    }


def parse_report_credits(text: str) -> dict:
    """Parse report-card module totals from the observed hidden inputs.

    The page renders the actual record rows client-side; module totals are
    server-rendered hidden inputs:
    credit0..4 = module hours, credit00..04 = module credits,
    flag = five position/total rank pairs, orderNum = module display order.
    """
    root = parse_html(text)
    hidden = _hidden_inputs(root)
    required = {
        *(f"credit{i}" for i in range(5)),
        *(f"credit0{i}" for i in range(5)),
        "flag",
        "orderNum",
    }
    if not required.issubset(hidden):
        raise ShiguangError("parse_error", "成绩单模块数据字段缺失或结构已变化。")

    order_parts = [part.strip() for part in hidden["orderNum"].split(",") if part.strip()]
    try:
        order = [int(part) for part in order_parts]
    except ValueError:
        raise ShiguangError("parse_error", "成绩单模块顺序不是有效整数。") from None
    if sorted(order) != [1, 2, 3, 4, 5]:
        raise ShiguangError("parse_error", "成绩单模块顺序不是 1..5 的完整排列。")

    flag_parts = [part.strip() for part in hidden["flag"].split(",")]
    ranks: list[dict | None] = []
    for index in range(5):
        position_raw = flag_parts[index * 2] if index * 2 < len(flag_parts) else ""
        total_raw = flag_parts[index * 2 + 1] if index * 2 + 1 < len(flag_parts) else ""
        if not position_raw or not total_raw:
            ranks.append(None)
            continue
        try:
            position = int(position_raw)
            total = int(total_raw)
        except ValueError:
            raise ShiguangError("parse_error", "成绩单专业排名不是有效整数。") from None
        ranks.append({"position": position, "total": total})

    modules = []
    for module_id, label, _codes in MODULES:
        credits = _decimal_text(hidden.get(f"credit0{module_id - 1}"), f"{label}学分")
        hours = _decimal_text(hidden.get(f"credit{module_id - 1}"), f"{label}学时")
        modules.append(
            {
                "module_id": module_id,
                "module": label,
                "credits_earned": credits,
                "hours": hours,
                "rank": ranks[module_id - 1],
                "required_credits": None,
                "remaining_credits": None,
                "credits_progress": None,
            }
        )
    modules.sort(key=lambda item: order.index(item["module_id"]))

    hour_values = [item["hours"] for item in modules]
    total_hours = None
    if all(value is not None for value in hour_values):
        total_hours = f"{sum((Decimal(value) for value in hour_values if value), Decimal(0)):.1f}"

    return {
        "modules": modules,
        "total_hours": total_hours,
        "total_hours_source": "sum_of_page_module_hours",
        "total_credits": None,
        "credits_total_note": "页面没有独立的可读总学分数值；不把模块学分相加冒充学校总学分。",
        "unknown_fields": [
            "required_credits",
            "remaining_credits",
            "credits_progress",
            "total_credits",
        ],
        "module_order": order,
    }


def parse_report_records(data: dict) -> dict:
    if not isinstance(data, dict):
        raise ShiguangError("parse_error", "成绩单接口返回结构不符合预期。")
    rows = data.get("gridModel")
    if not isinstance(rows, list):
        raise ShiguangError("parse_error", "成绩单接口缺少 gridModel 列表。")
    items = []
    for row in rows:
        if not isinstance(row, dict):
            raise ShiguangError("parse_error", "成绩单记录不是对象。")
        required = {"createTime", "hourName", "hourNum", "hourType", "remark"}
        if not required.issubset(row):
            raise ShiguangError("parse_error", "成绩单记录字段缺失或结构已变化。")
        hour_type = str(row.get("hourType") or "")
        module_id, module = MODULE_BY_CODE.get(hour_type, (None, None))
        raw_hours = row["hourNum"]
        if raw_hours is None:
            hours = None
        else:
            try:
                hours = float(raw_hours)
            except (TypeError, ValueError):
                raise ShiguangError("parse_error", "成绩单记录学时不是数值。") from None
            if hours != hours or hours in (float("inf"), float("-inf")):
                raise ShiguangError("parse_error", "成绩单记录学时不是有限数值。")
        items.append(
            {
                "module_id": module_id,
                "module": module,
                "module_code": hour_type or None,
                "create_time": (
                    str(row["createTime"]) if row["createTime"] is not None else None
                ),
                "name": str(row["hourName"]) if row["hourName"] is not None else None,
                "hours": hours,
                "remark": str(row["remark"]) if row["remark"] is not None else None,
                "recognition_status": None,
                "recognition_status_note": "接口只提供备注，没有独立认定状态字段。",
            }
        )
    return {
        "items": items,
        "record_count": len(items),
        "pagination": {
            "supported": False,
            "returned": len(items),
            "note": "成绩单页面一次返回全部代表性记录，没有分页控件。",
        },
        "official_export": {
            "client_pdf_button": True,
            "server_export_implemented": False,
            "note": "页面提供前端 html2canvas/jsPDF 导出按钮；本轮不生成或伪造官方 PDF。",
        },
    }


def _node_by_id(root: Node, ident: str) -> Node | None:
    for node in root.walk():
        if node.attrs.get("id") == ident:
            return node
    return None


def _page_time(value: str | None) -> datetime | None:
    if not value or not value.strip():
        return None
    raw = value.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def parse_registration_preview(
    text: str,
    activity_id: str,
    kind: str,
    now: datetime | None = None,
    strict: bool = False,
) -> dict:
    """Parse real detail-page registration state without submitting anything."""
    root = parse_html(text)
    hidden = _hidden_inputs(root)
    required = {
        "activityId",
        "enrollStartTime",
        "enrollEndTime",
        "activityStratTime",
        "activityEndTime",
    }
    if not required.issubset(hidden):
        if strict:
            raise ShiguangError(
                "registration_unavailable",
                "详情页没有完整的报名时间/活动字段，未提交报名。",
            )
        return {
            "status": "unavailable",
            "reason": "详情页没有完整的报名时间/活动字段。",
            "already_registered": None,
        }
    online_type = (hidden.get("onlineType") or hidden.get("activityOnlineType") or "").strip()
    if not online_type:
        if strict:
            raise ShiguangError(
                "registration_unavailable",
                "详情页没有活动类型字段，未提交报名。",
            )
        return {
            "status": "unavailable",
            "reason": "详情页没有活动类型字段。",
            "already_registered": None,
        }
    page_activity_id = hidden["activityId"].strip()
    if page_activity_id != str(activity_id):
        if strict:
            raise ShiguangError("parse_error", "详情页活动 ID 与请求不一致，未提交报名。")
        return {
            "status": "unavailable",
            "reason": "详情页活动 ID 与请求不一致。",
            "already_registered": None,
        }
    enroll_start = _page_time(hidden["enrollStartTime"])
    enroll_end = _page_time(hidden["enrollEndTime"])
    activity_start = _page_time(hidden["activityStratTime"])
    activity_end = _page_time(hidden["activityEndTime"])
    if enroll_end is None:
        if strict:
            raise ShiguangError(
                "registration_unavailable",
                "详情页报名结束时间不是可解析时间，未提交报名。",
            )
        return {
            "status": "unavailable",
            "reason": "详情页报名结束时间不是可解析时间。",
            "already_registered": None,
        }
    current = now or datetime.now()
    if activity_end is not None and current >= activity_end:
        status = "finished"
    elif activity_start is not None and current >= activity_start:
        status = "activity_in_progress"
    elif enroll_start is None:
        # Some social-practice pages have an unset/placeholder start time.
        status = "closed" if current >= enroll_end else "unavailable"
    elif current < enroll_start:
        status = "not_started"
    elif current >= enroll_end:
        status = "closed"
    else:
        status = "open"
    normal_supported = _node_by_id(root, "actvJoin") is not None
    invite_supported = _node_by_id(root, "actvJoinByCode") is not None
    if not normal_supported and not invite_supported:
        status = "unavailable"
    return {
        "activity_id": page_activity_id,
        "kind": kind,
        "status": status,
        "enroll_start": hidden["enrollStartTime"].strip(),
        "enroll_end": hidden["enrollEndTime"].strip(),
        "activity_start": hidden["activityStratTime"].strip(),
        "activity_end": hidden["activityEndTime"].strip(),
        "online_type": online_type,
        "normal_supported": normal_supported,
        "invite_code_supported": invite_supported,
        "reason_required": kind == "social_practice",
        "can_register": status == "open" and (normal_supported or invite_supported),
        "already_registered": None,
        "already_registered_note": "详情页没有稳定已报名状态字段；未调用未验证状态接口。",
    }


def parse_registration_response(data: dict, invite: bool) -> dict:
    oper = data.get("oper")
    # Keep only a bounded scalar token for exact known-code matching. Unknown
    # upstream text is never echoed into the tool result.
    oper_text = oper if isinstance(oper, str) and len(oper) <= 64 else None
    error_info = data.get("errorInfo")
    if error_info:
        return {
            "status": "requires_user_action",
            "submitted": False,
            "retry_recommended": False,
            "server_oper": None,
            "message": "报名信息未被接受，请按页面提示核对邀请码/报名资格；未自动重试。",
        }
    mapping = {
        "success": ("registered_pending_review", "报名成功，等待活动管理员审核。", True),
        "accepted": ("registered", "报名成功，等待活动开始。", True),
        "SincerityError": (
            "integrity_too_low",
            "诚信值低于 7 分，暂时不能报名新活动。",
            False,
        ),
        "moreNumAll": ("registration_busy", "报名人数较多，请稍后再试。", False),
        "moreNum": (
            "quota_full",
            "报名名额已满；普通报名可尝试邀请码入口。"
            if not invite
            else "邀请码报名名额已满。",
            False,
        ),
        "notRequest": ("eligibility_failed", "未满足该活动报名条件。", False),
        "timeRepeat": ("time_conflict", "该活动时间内已有其他活动。", False),
        "moreLockFail": (
            "registration_contention",
            "报名抢票失败/并发争用，未自动重试。",
            False,
        ),
        "beyondLimit": ("registration_limit", "超过同时报名名额限制。", False),
        "beyondLimit1": ("lecture_limit", "宣讲类活动最多只能同时报一个名额。", False),
    }
    if oper_text in mapping:
        status, message, submitted = mapping[oper_text]
        return {
            "status": status,
            "submitted": submitted,
            "retry_recommended": oper_text in {"moreNumAll", "moreLockFail"},
            "server_oper": oper_text,
            "message": message,
        }
    # An unrecognized code is not evidence of a definite rejection. Treat it
    # as an unknown submission outcome so the persistent receipt suppresses a
    # later POST; expose no raw upstream text.
    return {
        "status": "submission_unknown",
        "submitted": None,
        "retry_recommended": False,
        "server_oper": None,
        "server_oper_unrecognized": True,
        "verification_required": True,
        "message": "服务器返回未识别的报名业务码；未自动重试，也未猜测含义，请先核对本人参与记录。",
    }


class ShiguangClient:
    """Fixed-base, read-only client; the caller supplies the authenticated client."""

    def __init__(
        self,
        client: httpx.Client,
        base_url: str = BASE,
        timeout: httpx.Timeout | None = None,
    ):
        self.client = client
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout or DEFAULT_TIMEOUT

    def _get(self, path: str, params: dict | None = None) -> str:
        try:
            with self.client.stream(
                "GET",
                self.base_url + path,
                params=params,
                follow_redirects=False,
                timeout=self.timeout,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36 Edg/156.0.0.0"
                    ),
                },
            ) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location", "")
                    if "/cas/login" in location or "caslogin.jsp" in location:
                        raise ShiguangError("auth_required", "石光会话不可用，需要正常登录。")
                    raise ShiguangError("upstream_error", "石光返回了非预期的跳转。")
                if response.status_code == 401:
                    raise ShiguangError("auth_required", "石光会话不可用，需要正常登录。")
                if response.status_code == 403:
                    raise ShiguangError(
                        "access_denied",
                        "石光返回 403；没有明确登录标记，不触发 CAS 恢复。",
                    )
                if response.status_code != 200:
                    raise ShiguangError("upstream_error", f"石光返回 HTTP {response.status_code}。")
                content_type = response.headers.get("content-type", "")
                is_json = "json" in content_type
                limit = MAX_JSON_BYTES if is_json else MAX_HTML_BYTES
                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > limit:
                    raise ShiguangError("upstream_error", "石光响应超过本地大小限制。")
                content = bytearray()
                for chunk in response.iter_bytes():
                    if len(content) + len(chunk) > limit:
                        raise ShiguangError("upstream_error", "石光响应超过本地大小限制。")
                    content.extend(chunk)
                encoding = response.encoding or "utf-8"
                text = bytes(content).decode(encoding, errors="replace")
        except ShiguangError:
            raise
        except httpx.TimeoutException:
            raise ShiguangError("network_error", "石光请求超时。") from None
        except httpx.HTTPError:
            raise ShiguangError("network_error", "石光网络请求失败。") from None

        if is_json:
            try:
                data = json.loads(text)
            except ValueError:
                raise ShiguangError("upstream_error", "石光返回了无法解析的响应。") from None
            if not isinstance(data, dict):
                raise ShiguangError("upstream_error", "石光返回了非对象 JSON 响应。")
            if str(data.get("sessionOutFlag")).lower() == "true":
                raise ShiguangError("session_expired", "石光会话已过期。")
            raise ShiguangError("upstream_error", "石光返回了非预期的 JSON 响应。")
        if "html" not in content_type:
            raise ShiguangError("upstream_error", "石光返回了非 HTML 响应。")
        if _looks_like_login(text):
            raise ShiguangError("auth_required", "石光返回了登录页，需要正常登录。")
        return text

    def _post_json(self, path: str, data: dict, referer: str) -> dict:
        try:
            with self.client.stream(
                "POST",
                self.base_url + path,
                data=data,
                follow_redirects=False,
                timeout=self.timeout,
                headers={
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": referer,
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36 Edg/156.0.0.0"
                    ),
                },
            ) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location", "")
                    if "/cas/login" in location or "caslogin.jsp" in location:
                        raise ShiguangError("auth_required", "石光会话不可用，需要正常登录。")
                    raise ShiguangError("upstream_error", "石光返回了非预期的跳转。")
                if response.status_code == 401:
                    raise ShiguangError("auth_required", "石光会话不可用，需要正常登录。")
                if response.status_code == 403:
                    raise ShiguangError(
                        "access_denied",
                        "石光返回 403；没有明确登录标记，不触发 CAS 恢复。",
                    )
                if response.status_code != 200:
                    raise ShiguangError("upstream_error", f"石光返回 HTTP {response.status_code}。")
                content_type = response.headers.get("content-type", "")
                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > MAX_JSON_BYTES:
                    raise ShiguangError("upstream_error", "石光响应超过本地大小限制。")
                content = bytearray()
                for chunk in response.iter_bytes():
                    if len(content) + len(chunk) > MAX_JSON_BYTES:
                        raise ShiguangError("upstream_error", "石光响应超过本地大小限制。")
                    content.extend(chunk)
                encoding = response.encoding or "utf-8"
                text = bytes(content).decode(encoding, errors="replace")
        except ShiguangError:
            raise
        except httpx.TimeoutException:
            raise ShiguangError("network_error", "石光请求超时。") from None
        except httpx.HTTPError:
            raise ShiguangError("network_error", "石光网络请求失败。") from None

        if "json" not in content_type:
            if "html" in content_type and _looks_like_login(text):
                raise ShiguangError("auth_required", "石光返回了登录页，需要正常登录。")
            raise ShiguangError("upstream_error", "石光成绩单接口返回了非 JSON 响应。")
        try:
            data_obj = json.loads(text)
        except ValueError:
            raise ShiguangError("parse_error", "石光成绩单接口 JSON 无法解析。") from None
        if not isinstance(data_obj, dict):
            raise ShiguangError("parse_error", "石光成绩单接口返回了非对象 JSON。")
        if str(data_obj.get("sessionOutFlag")).lower() == "true":
            raise ShiguangError("session_expired", "石光会话已过期。")
        for key in ("actionErrors", "actionMessages", "errorMessages"):
            if data_obj.get(key):
                raise ShiguangError("upstream_error", "石光成绩单接口返回了业务错误。")
        if data_obj.get("errors"):
            raise ShiguangError("upstream_error", "石光成绩单接口返回了业务错误。")
        return data_obj

    def _post_registration(self, path: str, data: dict, referer: str) -> dict:
        try:
            with self.client.stream(
                "POST",
                self.base_url + path,
                data=data,
                follow_redirects=False,
                timeout=self.timeout,
                headers={
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": referer,
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36 Edg/156.0.0.0"
                    ),
                },
            ) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location", "")
                    if "/cas/login" in location or "caslogin.jsp" in location:
                        raise ShiguangError("auth_required", "石光会话不可用，报名未提交。")
                    raise ShiguangError(
                        "submission_unknown",
                        "报名请求返回了非预期跳转，结果未知；未自动重试。",
                    )
                if response.status_code == 401:
                    raise ShiguangError("auth_required", "石光会话不可用，报名未提交。")
                if response.status_code == 403:
                    raise ShiguangError("access_denied", "石光拒绝报名请求，未自动重试。")
                if response.status_code >= 500:
                    raise ShiguangError(
                        "submission_unknown",
                        "报名请求返回服务端错误，结果未知；未自动重试。",
                    )
                if response.status_code != 200:
                    raise ShiguangError(
                        "submission_rejected",
                        f"报名请求返回 HTTP {response.status_code}，未自动重试。",
                    )
                content_type = response.headers.get("content-type", "")
                content = bytearray()
                for chunk in response.iter_bytes():
                    if len(content) + len(chunk) > MAX_JSON_BYTES:
                        raise ShiguangError(
                            "submission_unknown",
                            "报名响应超过本地大小限制，结果未知；未自动重试。",
                        )
                    content.extend(chunk)
                encoding = response.encoding or "utf-8"
                text = bytes(content).decode(encoding, errors="replace")
        except ShiguangError:
            raise
        except (httpx.TimeoutException, httpx.HTTPError):
            raise ShiguangError(
                "submission_unknown",
                "报名请求超时或连接中断，结果未知；未自动重试。",
            ) from None
        if "json" not in content_type:
            if "html" in content_type and _looks_like_login(text):
                raise ShiguangError("auth_required", "石光返回登录页，报名未提交。")
            raise ShiguangError(
                "submission_unknown",
                "报名响应不是 JSON，结果未知；未自动重试。",
            )
        try:
            data_obj = json.loads(text)
        except ValueError:
            raise ShiguangError(
                "submission_unknown",
                "报名响应无法解析，结果未知；未自动重试。",
            ) from None
        if not isinstance(data_obj, dict):
            raise ShiguangError(
                "submission_unknown",
                "报名响应不是对象，结果未知；未自动重试。",
            )
        if str(data_obj.get("sessionOutFlag")).lower() == "true":
            raise ShiguangError("auth_required", "石光会话已过期，报名未确认提交。")
        if data_obj.get("actionErrors") or data_obj.get("errorMessages") or data_obj.get("errors"):
            raise ShiguangError(
                "submission_rejected",
                "报名请求被业务校验拒绝，未自动重试。",
            )
        return data_obj

    def registration_preview(self, activity_id: str, kind: str) -> dict:
        if not re.fullmatch(r"[0-9]{1,20}", activity_id or ""):
            raise ShiguangError("invalid_input", "activity_id 必须是数字。")
        if kind not in DETAIL_PATHS:
            raise ShiguangError("invalid_input", "kind 只能是 activity 或 social_practice。")
        text = self._get(
            DETAIL_PATHS[kind], {"actvModel.activityId": activity_id}
        )
        parsed = parse_detail(text)
        registration = parse_registration_preview(
            text, activity_id, kind, strict=True
        )
        return {
            "activity_id": activity_id,
            "kind": kind,
            "title": parsed["title"],
            "registration": registration,
        }

    def register_activity(
        self,
        activity_id: str,
        kind: str,
        invite_code: str | None = None,
        reason: str | None = None,
    ) -> dict:
        if not re.fullmatch(r"[0-9]{1,20}", activity_id or ""):
            raise ShiguangError("invalid_input", "activity_id 必须是数字。")
        if kind not in REGISTER_PATHS:
            raise ShiguangError("invalid_input", "kind 只能是 activity 或 social_practice。")
        if invite_code is not None:
            if not isinstance(invite_code, str) or not 1 <= len(invite_code) <= 200:
                raise ShiguangError("invalid_input", "邀请码长度必须为 1..200。")
            path = REGISTER_BY_CODE_PATH
            data = {
                "actvModel.activityId": activity_id,
                "inputInviteCode": invite_code,
            }
        else:
            path = REGISTER_PATHS[kind]
            data = {"actvModel.activityId": activity_id}
            if kind == "social_practice":
                if not isinstance(reason, str) or not 1 <= len(reason) <= 500:
                    raise ShiguangError("invalid_input", "社会实践报名理由必须为 1..500 字符。")
                data["actvModel.enrollVerifyDesc"] = reason
        referer = f"{self.base_url}{DETAIL_PATHS[kind]}?actvModel.activityId={activity_id}"
        response = self._post_registration(path, data, referer)
        parsed = parse_registration_response(response, invite=invite_code is not None)
        return {
            "activity_id": activity_id,
            "kind": kind,
            "invite_code_used": invite_code is not None,
            **parsed,
        }

    def credits(self) -> dict:
        parsed = parse_report_credits(self._get(REPORT_CARD_PATH))
        return {
            "status": "ok",
            "source_system": "shiguang",
            "report_type": "second_classroom_credit_progress",
            **parsed,
            "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }

    def report_card(self) -> dict:
        data = self._post_json(
            REPORT_CARD_AJAX_PATH,
            {},
            referer=f"{self.base_url}{REPORT_CARD_PATH}",
        )
        parsed = parse_report_records(data)
        return {
            "status": "ok",
            "source_system": "shiguang",
            "report_type": "second_classroom_report_card",
            **parsed,
            "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }

    def activities(
        self,
        page: int = 1,
        keyword: str | None = None,
        online_type: str | None = None,
        base_type: str | None = None,
    ) -> dict:
        if not isinstance(page, int) or isinstance(page, bool) or not 1 <= page <= 5000:
            raise ShiguangError("invalid_input", "page 必须在 1..5000。")
        if keyword is not None:
            if (
                not isinstance(keyword, str)
                or not 1 <= len(keyword) <= MAX_KEYWORD
                or not keyword.strip()
            ):
                raise ShiguangError("invalid_input", f"keyword 必须为 1..{MAX_KEYWORD} 个字符。")
        if online_type is not None and online_type not in ONLINE_TYPES:
            raise ShiguangError("invalid_input", "online_type 只支持 001..004。")
        if base_type is not None and base_type not in BASE_TYPES:
            raise ShiguangError("invalid_input", "base_type 只支持 001..005。")
        params: dict[str, str] = {"page": str(page), "oper": "1"}
        if keyword is not None:
            params["searchWord"] = keyword
        if online_type is not None:
            params["actvModel.activityOnlineType"] = online_type
        if base_type is not None:
            params["actvModel.activityBaseType"] = base_type
        parsed = parse_activity_list(self._get(LIST_PATH, params), page)
        return {
            "status": "ok",
            "source_system": "shiguang",
            "page": page,
            "total_pages": parsed["total_pages"],
            "next_page": parsed["next_page"],
            "items": parsed["items"],
            "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }

    def activity_detail(self, activity_id: str, kind: str = "activity") -> dict:
        if not re.fullmatch(r"[0-9]{1,20}", activity_id or ""):
            raise ShiguangError("invalid_input", "activity_id 必须是数字。")
        if kind not in DETAIL_PATHS:
            raise ShiguangError("invalid_input", "kind 只能是 activity 或 social_practice。")
        text = self._get(DETAIL_PATHS[kind], {"actvModel.activityId": activity_id})
        parsed = parse_detail(text)
        if not parsed["title"]:
            raise ShiguangError("parse_error", "未找到活动标题。")
        registration = parse_registration_preview(
            text, activity_id, kind, strict=False
        )
        return {
            "status": "ok",
            "source_system": "shiguang",
            "activity_id": activity_id,
            "kind": kind,
            **parsed,
            "registration": registration,
            "url": f"{BASE}{DETAIL_PATHS[kind]}?actvModel.activityId={activity_id}",
            "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }

    def my_participations(self, page: int = 1) -> dict:
        if not isinstance(page, int) or isinstance(page, bool) or not 1 <= page <= 500:
            raise ShiguangError("invalid_input", "page 必须在 1..500。")
        parsed = parse_activity_list(self._get(MY_JOIN_PATH, {"page": str(page)}), page)
        return {
            "status": "ok",
            "source_system": "shiguang",
            "page": page,
            "total_pages": parsed["total_pages"],
            "next_page": parsed["next_page"],
            "items": parsed["items"],
            "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
