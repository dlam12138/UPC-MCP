"""Stateless service boundary for the Tangdaowan electricity query.

This module deliberately has no state directory, credential store, cache,
history, default room or persistence layer.  The building and room are only
present in the current call and its result.  A service instance exists per MCP
server process and holds only an in-memory concurrency gate.
"""

from __future__ import annotations

import asyncio
from typing import Any

from .electricity import (
    CAMPUS,
    CAMPUS_ID,
    MAX_CONCURRENCY,
    Ambiguous,
    ElectricityClient,
    ElectricityError,
    Reading,
    now,
    validate_input,
)


class ElectricityService:
    def __init__(
        self,
        client: ElectricityClient | None = None,
        *,
        max_concurrency: int = MAX_CONCURRENCY,
    ):
        self.client = client or ElectricityClient()
        if (
            isinstance(max_concurrency, bool)
            or not isinstance(max_concurrency, int)
            or not 1 <= max_concurrency <= 4
        ):
            max_concurrency = MAX_CONCURRENCY
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def buildings(self) -> dict[str, Any]:
        """Read only the current school building-option directory."""
        async with self._semaphore:
            options = await self.client.fetch_buildings()
        return {
            "status": "ok",
            "source_system": "electricity",
            "campus": CAMPUS,
            "campus_id": CAMPUS_ID,
            "scope": "building_directory_only",
            "fetched_at": now(),
            "from_cache": False,
            "stale": False,
            "count": len(options),
            "buildings": [option.public() for option in options],
            "rooms_enumerated": False,
            "authentication": "anonymous_public_form",
            "persistence": "none",
            "warnings": [
                "楼号来自学校当前下发的表单选项，本项目不维护静态楼号映射。",
                "本工具只读取楼号目录，不查询任何房间，也不返回房间列表。",
            ],
            "limitations": [
                "学校页面结构或入口变化时返回 schema_changed/auth_required，"
                "不会伪造楼号。",
                "只支持唐岛湾校区；古镇口电费未接入。",
            ],
            "error": None,
        }

    async def query(self, building: object, room: object) -> dict[str, Any]:
        """Query exactly one explicit building/room pair; no default or history."""
        building_value, room_value = validate_input(building, room)
        async with self._semaphore:
            try:
                outcome = await self.client.query(building_value, room_value)
            except ElectricityError as error:
                if error.code == "room_not_found":
                    error.details.setdefault(
                        "query", self._query(building_value, room_value)
                    )
                raise
        if isinstance(outcome, Reading):
            return self._reading_result(building_value, room_value, outcome)
        if isinstance(outcome, Ambiguous):
            return self._ambiguous_result(building_value, room_value, outcome)
        raise ElectricityError(
            "local_error", "本地电量查询返回了未识别的结果，未返回数据。"
        )

    @staticmethod
    def _query(building: str, room: str) -> dict[str, str]:
        return {
            "campus": CAMPUS,
            "building": building,
            "room": room,
            "scope": "explicit_single_room",
        }

    @staticmethod
    def _source() -> dict[str, Any]:
        return {
            "system": "dfcz",
            "protocol": "aspnet_webforms_html",
            "transport": "http",
            "authentication": "anonymous_public_form",
            "redirects_followed": False,
            "retries": 0,
            "cache": "none",
            "room_match": "exact_building_and_room",
        }

    def _reading_result(
        self,
        building: str,
        room: str,
        reading: Reading,
    ) -> dict[str, Any]:
        warnings = [
            "剩余电量的单位是度（kWh），不是人民币余额；学校页面没有提供欠费金额。",
            "只有在学校返回唯一精确匹配且读数归属一致时才返回；候选不会自动选择。",
            "本次查询不保存楼号或房间号，不使用缓存、历史或默认宿舍。",
            "学校端点是匿名 HTTP 表单，楼号与房间号在传输中不经 TLS 保护。",
        ]
        if reading.kwh == 0:
            warnings.append(
                "0 度是有效的剩余电量读数，不等同于欠费或金额余额；"
                "页面未说明停电或欠费状态。"
            )
        return {
            "status": "ok",
            "source_system": "electricity",
            "campus": CAMPUS,
            "campus_id": CAMPUS_ID,
            "query": self._query(building, room),
            "fetched_at": now(),
            "from_cache": False,
            "stale": False,
            "reading_available": True,
            "data": reading.public(),
            "source": self._source(),
            "persistence": "none",
            "warnings": warnings,
            "limitations": [
                "数值来自学校公开 HTML 页面，不是稳定 JSON API；页面结构变化会返回 "
                "schema_changed。",
                "学校可能按房间字符串模糊匹配；本工具不采用第一项，也不把候选或"
                "其他房间的读数当作本次结果。",
                "同一房间可能对应多个系统编号；无法唯一确认时返回 ambiguous。",
                "不是现场计量、缴费、欠费或准入证明；不提供充值或缴费。",
                "学校页面没有提供电表更新时间；fetched_at 只是本次读取时间。",
                "只支持唐岛湾校区；古镇口电费未接入。",
                "本工具不枚举房间、不批量试号、不自动重试。",
            ],
            "next_action": "none",
            "error": None,
        }

    def _ambiguous_result(
        self,
        building: str,
        room: str,
        outcome: Ambiguous,
    ) -> dict[str, Any]:
        reasons = {
            "selection_required": "学校返回了候选房间，但没有可直接归属的读数。",
            "reading_unavailable": "学校返回了候选房间，但电量表结构不可用。",
            "reading_ownership_mismatch": "候选房间与电量表的系统编号或房间归属不一致。",
            "multiple_exact_candidates": "同一楼号/房间号对应多个系统编号。",
            "no_exact_candidate": "学校候选表中没有与输入完全一致的楼号/房间号。",
            "room_mismatch": "学校返回了另一个房间的读数，未当作本次结果。",
        }
        message = reasons.get(outcome.reason, "学校返回了无法唯一确认的房间记录。")
        return {
            "status": "ambiguous",
            "source_system": "electricity",
            "campus": CAMPUS,
            "campus_id": CAMPUS_ID,
            "query": self._query(building, room),
            "fetched_at": now(),
            "from_cache": False,
            "stale": False,
            "reading_available": False,
            "data": None,
            **outcome.public(),
            "unit": "kWh",
            "unit_label": "度",
            "source": self._source(),
            "persistence": "none",
            "warnings": [
                message,
                "本工具没有选择候选中的任何一项，也没有返回候选表之外的电量。",
                "学校可能按房间字符串模糊匹配；请让用户确认具体楼号、房间号和"
                "系统编号后再查询。",
                "本次查询不保存候选、楼号或房间号。",
            ],
            "limitations": [
                "本工具没有按系统编号选择或二次提交的契约，不能伪造“选择”请求。",
                "候选表没有返回可归属读数时，剩余电量保持未知，不能当作 0 度。",
                "学校端点是匿名 HTTP 表单，楼号与房间号在传输中不经 TLS 保护。",
                "只支持唐岛湾校区；古镇口电费未接入。",
            ],
            "next_action": "ask_user_to_confirm_exact_room",
            "error": None,
        }
