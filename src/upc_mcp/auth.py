import json
import re
from datetime import datetime
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs

from pydantic import SecretStr, ValidationError

from .errors import UPCError
from .jhzf import BILL_ENDPOINT, BILL_PAGE, FORM_FIELDS, ORIGIN, JhzfClient, parse_bill
from .models import Session, now
from .storage import SessionStore, unprotect


def candidate(
    form_text: str,
    cookie_header: str,
    original_headers: dict,
    response_text: str,
    source="normal_cdp",
) -> Session:
    try:
        fields = parse_qs(form_text, keep_blank_values=True, strict_parsing=True)
        if set(fields) != FORM_FIELDS or any(len(v) != 1 for v in fields.values()):
            raise ValueError
        form = {k: v[0] for k, v in fields.items()}
        if (
            form["xxbh"] != "synjones"
            or form["pagesize"] != "10"
            or not re.fullmatch(r"[0-9]{1,3}", form["curpage"])
            or not 1 <= int(form["curpage"]) <= 100
            or len(form["acctype"]) > 64
        ):
            raise ValueError
        start, end = [datetime.strptime(form[k], "%Y%m%d") for k in ("query_start", "query_end")]
        if start > end or (start.year, start.month) != (end.year, end.month):
            raise ValueError
        cookies = SimpleCookie()
        cookies.load(cookie_header)
        value = cookies["JSESSIONID"].value
        if not re.fullmatch(r"[A-Za-z0-9._-]{8,256}", value):
            raise ValueError
        allowed = {
            "accept": "Accept",
            "user-agent": "User-Agent",
            "x-requested-with": "X-Requested-With",
        }
        headers = {}
        for key, value_header in original_headers.items():
            if key.lower() in allowed:
                if (
                    not isinstance(value_header, str)
                    or len(value_header) > 1024
                    or "\r" in value_header
                    or "\n" in value_header
                ):
                    raise ValueError
                headers[allowed[key.lower()]] = value_header
        headers.update({"Origin": ORIGIN, "Referer": BILL_PAGE})
        session = Session(
            student_id=form["sno"],
            account=form["account"],
            cookie=SecretStr("JSESSIONID=" + value),
            headers=headers,
            seed=form,
            source=source,
        )
        parse_bill(response_text, session.account, int(form["curpage"]))
        return session
    except UPCError:
        raise
    except (ValueError, KeyError, TypeError, ValidationError):
        raise UPCError("auth_required", "未取得结构、本人目标和业务结果一致的授权记录。") from None


def from_recording(path: Path) -> Session:
    """Local operator-only bootstrap from a previously authorized recording."""
    if path.suffix != ".dpapi" or not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
        raise UPCError("invalid_input", "请选择本机 DPAPI 加密的原始请求记录。")
    try:
        items = json.loads(unprotect(path.read_bytes()))
        if not isinstance(items, list):
            raise ValueError
        for item in items:
            request = item.get("request", {})
            if (
                request.get("method") != "POST"
                or request.get("url") != BILL_ENDPOINT
                or item.get("response", {}).get("status") != 200
            ):
                continue
            body = item.get("body", item.get("responseBody", ""))
            try:
                if str(json.loads(body).get("retcode")) != "0":
                    continue
            except (ValueError, AttributeError):
                continue
            headers = {**request.get("headers", {}), **item.get("extra", {}).get("headers", {})}
            cookie = next((v for k, v in headers.items() if k.lower() == "cookie"), "")
            return candidate(
                request.get("postData", ""), cookie, headers, body, source="normal_cdp_recording"
            )
    except UPCError:
        raise
    except (ValueError, TypeError, AttributeError):
        raise UPCError("invalid_input", "授权记录格式不受支持。") from None
    raise UPCError("auth_required", "记录中没有本人成功流水及对应的业务会话。")


async def adopt(session: Session, store: SessionStore, client: JhzfClient | None = None) -> dict:
    client = client or JhzfClient()
    await client.verify_owner(session)
    result = await client.bill(session, session.seed)
    session.verified_at = now()
    store.replace(session)  # Invalid/expired candidates never replace the previous session.
    return {
        "status": "ok",
        "session": session.public(),
        "verified_records": len(result["transactions"]),
    }
