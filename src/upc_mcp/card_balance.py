"""Anonymous read-only UPC campus-card balance protocol.

This module deliberately has no dependency on JHZF sessions, CAS, WeChat,
cookies, the retired bill flow, or Shiguang. It only sends the locally
configured student number to the one observed balance endpoint.

Verified upstream facts (from the owner's Kotlin client and its real use):

* ``POST http://jhzf.upc.edu.cn:20081/wechat/callinterface/getCardInfo.html``
* form: ``sno=<id>&xxbh=synjones&idtype=sno&id=<id>``
* the response is JSON; ``retcode`` is ``"0"`` and ``card`` is a one-element
  array whose ``sno`` equals the queried student number;
* ``db_balance`` and ``unsettle_amount`` are signed integer **cents**;
* ``Accept: application/json`` was observed to return HTTP 406, so ``*/*`` is
  sent instead. No Cookie, CAS ticket, or WeChat session is involved.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.parse import quote_plus

import httpx

ENDPOINT = "http://jhzf.upc.edu.cn:20081/wechat/callinterface/getCardInfo.html"
MAX_RESPONSE_BYTES = 256 * 1024
MAX_CENT = 2**63 - 1
MIN_CENT = -(2**63)
STUDENT_ID_RE = re.compile(r"[0-9]{8,16}\Z")
AMOUNT_RE = re.compile(r"-?[0-9]+\Z")
DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
REQUEST_HEADERS = {
    "Accept": "*/*",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Cache-Control": "no-cache, no-store",
    "User-Agent": "UPC-MCP/0.1",
}


class CardBalanceError(Exception):
    """Fixed, credential-free error crossing the adapter/service boundary."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)

    def payload(self) -> dict:
        return {
            "status": self.code,
            "source_system": "card_balance",
            "error": {"message": str(self)},
        }


def valid_student_id(value: object) -> bool:
    """The server form accepts 8-16 ASCII digits; keep the same local limit."""
    return isinstance(value, str) and STUDENT_ID_RE.fullmatch(value) is not None


def build_form(student_id: str) -> str:
    """Build the exact observed form body without accepting a caller URL."""
    if not valid_student_id(student_id):
        raise CardBalanceError("invalid_input", "本地学号配置应为 8–16 位数字。")
    encoded = quote_plus(student_id, safe="")
    return f"sno={encoded}&xxbh=synjones&idtype=sno&id={encoded}"


def _format_cents(value: Any) -> Decimal:
    """Convert one signed integer-cent field to an exact two-decimal Decimal."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise CardBalanceError("schema_changed", "学校返回的金额字段类型已变化。")
    raw = str(value)
    if AMOUNT_RE.fullmatch(raw) is None:
        raise CardBalanceError("schema_changed", "学校返回的金额不再是整数分。")
    cents = int(raw)
    if not MIN_CENT <= cents <= MAX_CENT:
        raise CardBalanceError("schema_changed", "学校返回的金额超出本机可精确表示的范围。")
    # The upstream unit is an integer cent; scaleb is exact and keeps 2 places.
    return Decimal(cents).scaleb(-2)


def format_amount(value: Decimal) -> str:
    """Serialize a Decimal without float conversion or scientific notation."""
    return format(value, "f")


def _reject_constant(_value: str) -> None:
    # NaN/Infinity are not valid money values.
    raise ValueError


@dataclass(frozen=True)
class CardBalanceSnapshot:
    """One validated single-card balance result; never contains the student id."""

    balance: Decimal
    unsettle_amount: Decimal
    card_count: int = 1
    validated_fields: tuple[str, ...] = (
        "retcode=0",
        "card.count=1",
        "card[0].sno",
        "card[0].db_balance",
        "card[0].unsettle_amount",
    )

    def public(self) -> dict:
        return {
            "card_count": self.card_count,
            "balance": format_amount(self.balance),
            "unsettle_amount": format_amount(self.unsettle_amount),
            "currency": "CNY",
            "amount_unit": "CNY",
            "upstream_amount_unit": "CNY_cent",
            "validated_fields": list(self.validated_fields),
        }


def parse_card_balance(text: str, expected_id: str) -> CardBalanceSnapshot:
    """Parse one observed response and enforce the full owner contract.

    Empty cards, multiple cards, a different ``sno``, a business failure, and
    malformed/missing money fields are distinct errors. None of them is turned
    into a zero balance.
    """
    if not valid_student_id(expected_id):
        raise CardBalanceError("invalid_input", "本地学号配置应为 8–16 位数字。")
    try:
        root = json.loads(text, parse_float=Decimal, parse_constant=_reject_constant)
    except (TypeError, ValueError):
        raise CardBalanceError("schema_changed", "学校返回的余额响应不是有效 JSON。") from None
    if not isinstance(root, dict):
        raise CardBalanceError("schema_changed", "学校返回的余额响应结构已变化。")
    if "retcode" not in root:
        raise CardBalanceError("schema_changed", "学校返回的余额响应缺少状态字段。")
    retcode = root["retcode"]
    if isinstance(retcode, bool) or not isinstance(retcode, (str, int)):
        raise CardBalanceError("schema_changed", "学校返回的状态字段类型已变化。")
    if str(retcode) != "0":
        raise CardBalanceError("upstream_error", "学校余额接口返回业务失败。")
    if "card" not in root:
        raise CardBalanceError("schema_changed", "学校返回的余额响应缺少卡列表。")
    cards = root["card"]
    if not isinstance(cards, list):
        raise CardBalanceError("schema_changed", "学校返回的卡列表结构已变化。")
    if not cards:
        raise CardBalanceError("card_empty", "学校返回的本人卡列表为空。")
    if len(cards) != 1:
        raise CardBalanceError(
            "card_multiple",
            "学校返回多张卡，本工具不猜测应使用哪一张，未返回余额。",
        )
    card = cards[0]
    if not isinstance(card, dict):
        raise CardBalanceError("schema_changed", "学校返回的卡记录结构已变化。")
    sno = card.get("sno")
    if isinstance(sno, bool) or not isinstance(sno, (str, int)):
        raise CardBalanceError("schema_changed", "学校返回的卡记录缺少本人学号。")
    if str(sno) != expected_id:
        raise CardBalanceError("identity_mismatch", "学校返回的卡记录不属于本地配置的本人。")
    balance = _format_cents(card.get("db_balance"))
    unsettle = _format_cents(card.get("unsettle_amount"))
    return CardBalanceSnapshot(balance=balance, unsettle_amount=unsettle)


class CardBalanceClient:
    """HTTP adapter for the one fixed anonymous balance endpoint."""

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: httpx.Timeout | None = None,
    ):
        self.transport = transport
        self.timeout = timeout or DEFAULT_TIMEOUT

    async def fetch(self, student_id: str) -> CardBalanceSnapshot:
        form = build_form(student_id)
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                trust_env=False,
                follow_redirects=False,
                timeout=self.timeout,
            ) as client:
                async with client.stream(
                    "POST",
                    ENDPOINT,
                    content=form.encode("utf-8"),
                    headers=REQUEST_HEADERS,
                ) as response:
                    if response.status_code in (401, 403):
                        raise CardBalanceError(
                            "access_denied",
                            "学校余额接口拒绝了匿名请求，未继续重试。",
                        )
                    if response.status_code == 429:
                        raise CardBalanceError(
                            "rate_limited",
                            "学校余额接口暂时限流，未继续重试。",
                        )
                    if not 200 <= response.status_code <= 299:
                        raise CardBalanceError(
                            "upstream_error",
                            f"学校余额接口返回 HTTP {response.status_code}。",
                        )
                    declared = response.headers.get("content-length")
                    if declared is not None:
                        try:
                            declared_size = int(declared)
                        except ValueError:
                            declared_size = None
                        if declared_size is not None and declared_size > MAX_RESPONSE_BYTES:
                            raise CardBalanceError(
                                "response_too_large",
                                "学校余额响应超过本工具大小上限。",
                            )
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise CardBalanceError(
                                "response_too_large",
                                "学校余额响应超过本工具大小上限。",
                            )
                        body.extend(chunk)
        except CardBalanceError:
            raise
        except (httpx.HTTPError, OSError):
            raise CardBalanceError(
                "network_error",
                "无法连接学校校园卡余额服务，请检查网络后重试。",
            ) from None
        try:
            text = bytes(body).decode("utf-8-sig")
        except UnicodeDecodeError:
            raise CardBalanceError("network_error", "学校余额响应编码无效。") from None
        return parse_card_balance(text, student_id)
