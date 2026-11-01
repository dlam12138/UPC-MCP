"""Local validation, concurrency, filtering, and pagination for classrooms.

The protocol/HTTP adapter is :mod:`upc_mcp.classrooms`.  This service module
contains no credentials and no school-specific storage; it only turns one
validated public response into a bounded, explicit MCP result.
"""

from __future__ import annotations

import asyncio
from typing import Any

from .classrooms import (
    DEFAULT_ROOM_LIMIT,
    MAX_CONCURRENCY,
    MAX_ROOM_LIMIT,
    MAX_ROOM_OFFSET,
    PERIOD_GROUPS,
    ClassroomDirectory,
    ClassroomError,
    ClassroomQuery,
    FreeClassroomClient,
    now,
)


class ClassroomService:
    """Validate one query, bound concurrency, and paginate room output."""

    def __init__(
        self,
        client: FreeClassroomClient | None = None,
        *,
        max_concurrency: int = MAX_CONCURRENCY,
    ):
        self.client = client or FreeClassroomClient()
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def query(
        self,
        *,
        campus: object,
        date: object,
        periods: object,
        building: object = "全部",
        only_available: object = False,
        offset: object = 0,
        limit: object = DEFAULT_ROOM_LIMIT,
    ) -> dict[str, Any]:
        query = ClassroomQuery.create(
            campus=campus,
            date=date,
            periods=periods,
            building=building,
        )
        if not isinstance(only_available, bool):
            raise ClassroomError("invalid_input", "only_available 必须是布尔值。")
        offset_value = _pagination_integer(
            offset, "offset", minimum=0, maximum=MAX_ROOM_OFFSET
        )
        limit_value = _pagination_integer(
            limit, "limit", minimum=1, maximum=MAX_ROOM_LIMIT
        )

        async with self._semaphore:
            try:
                directory = await self.client.fetch(query)
            except ClassroomError as error:
                error.details.setdefault(
                    "query", {**query.public(), "only_available": only_available}
                )
                raise

        return self._result(
            query,
            directory,
            only_available=only_available,
            offset=offset_value,
            limit=limit_value,
        )

    def _result(
        self,
        query: ClassroomQuery,
        directory: ClassroomDirectory,
        *,
        only_available: bool,
        offset: int,
        limit: int,
    ) -> dict[str, Any]:
        query_output = {**query.public(), "only_available": only_available}
        buildings = directory.public_buildings()
        building_options = directory.public_building_options()

        if query.building != "全部":
            if query.building not in buildings:
                raise ClassroomError(
                    "invalid_building",
                    "学校未返回该教学楼；请使用 building_options 中的 id 或“全部”。",
                    details={
                        "query": query_output,
                        "buildings": buildings,
                        "building_options": building_options,
                    },
                )
            if any(room.building != query.building for room in directory.rooms):
                raise ClassroomError(
                    "schema_changed",
                    "学校返回了与所选教学楼不一致的教室。",
                )

        if not directory.rooms and query.building == "全部":
            raise ClassroomError(
                "empty_directory",
                "学校返回了空教室目录；不能解释为全部空闲。",
                details={
                    "query": query_output,
                    "buildings": buildings,
                    "building_options": building_options,
                    "directory_empty": True,
                },
            )

        rooms = list(directory.rooms)
        available_rooms = [room for room in rooms if not room.occupied]
        occupied_count = len(rooms) - len(available_rooms)
        matched = available_rooms if only_available else rooms
        page = matched[offset : offset + limit]
        has_more = offset + len(page) < len(matched)
        next_offset = offset + len(page) if has_more else None

        if not rooms:
            status = "empty"
            reason = "no_rooms_for_building"
            availability = "no_rooms"
        elif not matched:
            status = "empty"
            reason = "all_occupied" if not available_rooms else "no_matching_rooms"
            availability = "all_occupied" if not available_rooms else "available"
        elif offset >= len(matched):
            status = "empty"
            reason = "offset_out_of_range"
            availability = "available" if available_rooms else "all_occupied"
        else:
            status = "ok"
            reason = None
            availability = "available" if available_rooms else "all_occupied"

        limitations = [
            "占用状态是所选节次组合的聚合：任一所选节次被占用即 occupied=true；"
            "无法从该聚合结果恢复每节独立状态。",
            "上午 1–5、下午 6–9、晚上 10–13 仅是学校页面的节次分组，"
            "本工具不推断具体钟点时间。",
            "capacity 来自学校返回的 num；缺失或非正数时为 null，未从其他来源推断。",
            "一次查询只返回本次请求的数据，不使用缓存、后台刷新或定时抓取。",
            "教室目录和占用是公开页面数据，不代表预约、占座或选课结果。",
        ]
        if has_more:
            limitations.append(
                "结果已按 limit 分页截断；counts.matched 是过滤后的总数，"
                "本次只返回其中一部分，请用 offset/next_offset 继续。"
            )

        return {
            "status": status,
            "source_system": "classrooms",
            "query": query_output,
            "fetched_at": now(),
            "from_cache": False,
            "stale": False,
            "buildings": buildings,
            "building_options": building_options,
            "period_groups": {
                label: list(periods) for label, periods in PERIOD_GROUPS.items()
            },
            "counts": {
                "total": len(rooms),
                "available": len(available_rooms),
                "occupied": occupied_count,
                "matched": len(matched),
                "returned": len(page),
            },
            "pagination": {
                "offset": offset,
                "limit": limit,
                "total": len(matched),
                "returned": len(page),
                "has_more": has_more,
                "next_offset": next_offset,
            },
            "truncated": has_more,
            "availability": availability,
            "reason": reason,
            "rooms": [room.public() for room in page],
            "limitations": limitations,
            "error": None,
        }


def _pagination_integer(
    value: object,
    name: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise ClassroomError(
            "invalid_pagination",
            f"{name} 必须是 {minimum}–{maximum} 的整数。",
        )
    return value
