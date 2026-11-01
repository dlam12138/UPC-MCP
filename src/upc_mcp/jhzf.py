import asyncio
import calendar
import json
import re
from datetime import date, datetime

import httpx

from .errors import UPCError
from .models import Session, now
from .storage import SessionStore

ORIGIN = "http://jhzf.upc.edu.cn:20081"
BILL_PAGE = ORIGIN + "/wechat/bill/my.html"
BILL_ENDPOINT = ORIGIN + "/wechat/callinterface/queryHisTotal.html"
BALANCE_ENDPOINT = ORIGIN + "/wechat/callinterface/getCardInfo.html"
FORM_FIELDS = {
    "sno",
    "xxbh",
    "curpage",
    "pagesize",
    "account",
    "acctype",
    "query_start",
    "query_end",
}


def integer(value, *, signed=False) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise UPCError("schema_changed", "学校返回的整数字段类型已变化。")
    if not re.fullmatch(r"[+-]?\d+" if signed else r"\d+", str(value)):
        raise UPCError("schema_changed", "学校返回的整数字段格式已变化。")
    return int(value)


def parse_bill(text: str, account: str, page: int) -> dict:
    try:
        root = json.loads(text)
        if not isinstance(root, dict):
            raise ValueError
        if str(root.get("retcode")) != "0":
            raise UPCError("upstream_error", "学校流水接口返回业务失败。")
        rows = root["total"]
        if not isinstance(rows, list) or len(rows) > 10:
            raise ValueError
        next_page = integer(root["nextpage"])
        if next_page != 0 and next_page <= page:
            raise ValueError
        result = []
        for row in rows:
            if not isinstance(row, dict) or str(row.get("account")) != account:
                raise ValueError
            timestamp = row["occtime"]
            if not isinstance(timestamp, str) or not re.fullmatch(r"\d{14}", timestamp):
                raise ValueError
            datetime.strptime(timestamp, "%Y%m%d%H%M%S")
            for key in ("tranname", "mercname", "acctype"):
                if not isinstance(row.get(key), (str, int)) or isinstance(row.get(key), bool):
                    raise ValueError
            result.append(
                {
                    "occurred_at": timestamp,
                    "amount_cents": integer(row["sign_tranamt"], signed=True),
                    "kind": str(row["tranname"]),
                    "merchant": str(row["mercname"]),
                    "account_type": str(row["acctype"]),
                }
            )
        return {
            "transactions": result,
            "next_page": next_page,
            "upstream_rowcount": integer(root["rowcount"]),
            "amount_unit": "CNY_cent",
            "timestamp_format": "yyyyMMddHHmmss",
            "upstream_timezone": None,
        }
    except UPCError:
        raise
    except (ValueError, KeyError, TypeError, OverflowError):
        raise UPCError("schema_changed", "流水结构或记录归属不符合已验证契约。") from None


def month_form(session: Session, month: str | None, page: int) -> tuple[str, dict[str, str]]:
    today = date.today()
    month = month or today.strftime("%Y-%m")
    if not re.fullmatch(r"\d{4}-\d{2}", month) or not 1 <= page <= 100:
        raise UPCError("invalid_input", "月份应为 YYYY-MM，页码范围为 1–100。")
    try:
        year, number = map(int, month.split("-"))
        start = date(year, number, 1)
        end = min(date(year, number, calendar.monthrange(year, number)[1]), today)
    except ValueError:
        raise UPCError("invalid_input", "月份无效。") from None
    if start > today or year < 2000:
        raise UPCError("invalid_input", "只支持 2000 年至当前月份的本人查询。")
    return month, {
        "sno": session.student_id,
        "xxbh": "synjones",
        "curpage": str(page),
        "pagesize": "10",
        "account": session.account,
        "acctype": session.seed.get("acctype", ""),
        "query_start": start.strftime("%Y%m%d"),
        "query_end": end.strftime("%Y%m%d"),
    }


class JhzfClient:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self.transport = transport

    async def post(self, endpoint: str, form: dict, headers: dict, cookie: str = "") -> str:
        if endpoint not in {BILL_ENDPOINT, BALANCE_ENDPOINT}:
            raise UPCError("unsupported", "请求目标未列入允许的学校接口。")
        request_headers = {
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            **headers,
        }
        if cookie:
            request_headers["Cookie"] = cookie
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(20, connect=10),
            ) as client:
                async with client.stream(
                    "POST", endpoint, data=form, headers=request_headers
                ) as response:
                    if response.status_code in (401, 403):
                        raise UPCError("auth_expired", "学校拒绝当前业务会话，请重新授权。")
                    if response.status_code != 200:
                        raise UPCError("upstream_error", f"学校返回 HTTP {response.status_code}。")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 1024 * 1024:
                            raise UPCError("schema_changed", "学校响应超过本工具大小上限。")
                    return bytes(body).decode("utf-8-sig")
        except UPCError:
            raise
        except (httpx.HTTPError, UnicodeError):
            raise UPCError("network_error", "学校请求失败或响应编码无效。") from None

    async def bill(self, session: Session, form: dict) -> dict:
        body = await self.post(
            BILL_ENDPOINT, form, session.headers, session.cookie.get_secret_value()
        )
        return parse_bill(body, session.account, int(form["curpage"]))

    async def verify_owner(self, session: Session):
        form = {
            "sno": session.student_id,
            "xxbh": "synjones",
            "idtype": "sno",
            "id": session.student_id,
        }
        text = await self.post(BALANCE_ENDPOINT, form, {})
        try:
            root = json.loads(text)
            cards = root["card"]
            if (
                str(root["retcode"]) != "0"
                or not isinstance(cards, list)
                or len(cards) != 1
                or str(cards[0]["sno"]) != session.student_id
                or str(cards[0]["account"]) != session.account
            ):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise UPCError("identity_mismatch", "学校本人学号与卡账户映射未通过核对。") from None


class BillService:
    def __init__(self, store: SessionStore, client: JhzfClient | None = None):
        self.store, self.client = store, client or JhzfClient()
        self.lock = asyncio.Lock()

    async def query(self, month: str | None = None, page: int = 1, *, check=False) -> dict:
        async with self.lock:
            session = self.store.load()
            if session is None:
                raise UPCError("auth_required", "请在本地命令行完成本人账单授权。")
            if session.state == "auth_expired":
                raise UPCError("auth_expired", "当前业务会话已被学校拒绝，请重新授权。")
            month, form = month_form(session, month, page)
            if check:
                form = session.seed
                month = form["query_start"][:4] + "-" + form["query_start"][4:6]
                page = int(form["curpage"])
            try:
                data = await self.client.bill(session, form)
            except UPCError as error:
                state = "auth_expired" if error.code == "auth_expired" else "unverified"
                self.store.finish(session.revision, state)
                raise
            self.store.finish(session.revision)
            return {
                "status": "ok" if data["transactions"] else "empty",
                "source_system": "jhzf",
                "query": {"session_ref": "self", "month": month, "page": page},
                "fetched_at": now(),
                "from_cache": False,
                "stale": False,
                "data": data,
                "error": None,
            }
