"""Local validation, concurrency, filtering, and pagination for classrooms.

The protocol/HTTP adapter is :mod:`upc_mcp.classrooms`.  This service module
contains no credentials and no school-specific storage; it turns validated
public responses into bounded, explicit MCP results.

Two scopes are exposed:

* ``overview`` — one ``search-all`` for a campus, per-building counts only;
* ``full`` — one or two buildings (or every building) verified room by room
  through the official ``detail`` endpoint.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

from .classrooms import (
    CONNECT_TIMEOUT_SECONDS,
    DEFAULT_ROOM_LIMIT,
    DETAIL_CONCURRENCY,
    DETAIL_DEADLINE_SECONDS,
    MAX_CONCURRENCY,
    MAX_DETAIL_REQUESTS,
    MAX_ROOM_LIMIT,
    MAX_ROOM_OFFSET,
    PERIOD_GROUPS,
    READ_TIMEOUT_SECONDS,
    ClassroomDirectory,
    ClassroomError,
    ClassroomQuery,
    ClassroomVerification,
    FreeClassroomClient,
    is_reserved_name,
    now,
)

FULL_DETAIL_DEADLINE_SECONDS = 60.0
MAX_SCOPE_BUILDINGS = 2
MAX_SCOPE_ROOMS = MAX_DETAIL_REQUESTS
MAX_TOTAL_SCOPE_REQUESTS = MAX_DETAIL_REQUESTS + MAX_SCOPE_BUILDINGS + 1
SEARCH_ALL_TIMEOUT_SECONDS = CONNECT_TIMEOUT_SECONDS + READ_TIMEOUT_SECONDS
SCOPE_MODES = {"auto", "overview", "full"}


class ClassroomService:
    """Validate one query, bound school concurrency, verify, and paginate."""

    def __init__(
        self,
        client: FreeClassroomClient | None = None,
        *,
        max_concurrency: int = MAX_CONCURRENCY,
        detail_concurrency: int = DETAIL_CONCURRENCY,
        detail_deadline: float = DETAIL_DEADLINE_SECONDS,
        full_detail_deadline: float = FULL_DETAIL_DEADLINE_SECONDS,
    ):
        self.client = client or FreeClassroomClient()
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self.detail_concurrency = detail_concurrency
        self.detail_deadline = detail_deadline
        self.full_detail_deadline = full_detail_deadline

    # ------------------------------------------------------------------
    # Legacy single-scope query (candidate verification)
    # ------------------------------------------------------------------
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

        try:
            directory = await self.client.fetch(query, gate=self._semaphore)
        except ClassroomError as error:
            error.details.setdefault(
                "query", {**query.public(), "only_available": only_available}
            )
            raise
        self._validate(query, directory)
        directory, verification = await self._verify_rooms(
            query,
            directory,
            verify_all=False,
            deadline=self.detail_deadline,
        )

        return self._result(
            query,
            directory,
            verification,
            only_available=only_available,
            offset=offset_value,
            limit=limit_value,
            mode="single",
            selected_buildings=[query.building],
            request_budget=self._request_budget(
                search_count=1,
                verification=verification,
                mode="single",
                deadline=self.detail_deadline,
            ),
        )

    # ------------------------------------------------------------------
    # Scope-aware query (overview / full)
    # ------------------------------------------------------------------
    async def query_scope(
        self,
        *,
        campus: object,
        date: object,
        periods: object,
        building: object = "全部",
        buildings: object = None,
        mode: object = "auto",
        only_available: object = False,
        include_conflicts: object = False,
        offset: object = 0,
        limit: object = DEFAULT_ROOM_LIMIT,
    ) -> dict[str, Any]:
        base = ClassroomQuery.create(
            campus=campus,
            date=date,
            periods=periods,
            building="全部",
        )
        if not isinstance(only_available, bool):
            raise ClassroomError("invalid_input", "only_available 必须是布尔值。")
        if not isinstance(include_conflicts, bool):
            raise ClassroomError("invalid_input", "include_conflicts 必须是布尔值。")
        offset_value = _pagination_integer(
            offset, "offset", minimum=0, maximum=MAX_ROOM_OFFSET
        )
        limit_value = _pagination_integer(
            limit, "limit", minimum=1, maximum=MAX_ROOM_LIMIT
        )
        resolved_mode, selected = self._resolve_scope(building, buildings, mode)
        try:
            if resolved_mode == "overview":
                directory = await self.client.fetch(base, gate=self._semaphore)
                self._validate(base, directory)
                return self._overview_result(
                    base,
                    directory,
                    selected,
                    only_available=only_available,
                    offset=offset_value,
                    limit=limit_value,
                )
            directories = await self._fetch_scope(base, selected)
        except ClassroomError as error:
            error.details.setdefault(
                "query",
                {
                    **base.public(),
                    "only_available": only_available,
                    "mode": resolved_mode,
                },
            )
            raise

        merged = self._merge_directories(directories)
        if len(merged.rooms) > MAX_SCOPE_ROOMS:
            raise ClassroomError(
                "scope_too_large",
                "所选范围的房间数超过本地逐间校验上限；未发起 detail 请求。",
                details={
                    "query": base.public(),
                    "rooms": len(merged.rooms),
                    "limit": MAX_SCOPE_ROOMS,
                },
            )
        search_count = 1 if selected is None else len(selected)
        requested_total = search_count + len(merged.rooms)
        if requested_total > MAX_TOTAL_SCOPE_REQUESTS:
            raise ClassroomError(
                "request_budget_exceeded",
                "所选范围的计划请求数超过本地总预算；未发起 detail 请求。",
                details={
                    "query": base.public(),
                    "requested": requested_total,
                    "limit": MAX_TOTAL_SCOPE_REQUESTS,
                },
            )
        merged, verification = await self._verify_rooms(
            base,
            merged,
            verify_all=True,
            deadline=self.full_detail_deadline,
        )
        summaries = self._building_summaries(merged)
        return self._result(
            base,
            merged,
            verification,
            only_available=only_available,
            offset=offset_value,
            limit=limit_value,
            mode="full",
            selected_buildings=selected or ["全部"],
            building_summaries=summaries,
            include_conflicts=include_conflicts,
            request_budget=self._request_budget(
                search_count=search_count,
                verification=verification,
                mode="full",
                deadline=self.full_detail_deadline,
            ),
        )

    @staticmethod
    def _resolve_scope(
        building: object, buildings: object, mode: object
    ) -> tuple[str, list[str] | None]:
        if not isinstance(mode, str) or mode not in SCOPE_MODES:
            raise ClassroomError(
                "invalid_mode", "mode 必须是 auto、overview 或 full。"
            )
        if not isinstance(building, str):
            raise ClassroomError("invalid_building", "教学楼必须是“全部”或学校返回的楼标识。")
        current = building.strip()
        if not current:
            raise ClassroomError("invalid_building", "教学楼不能为空。")

        selected: list[str] | None = None
        all_requested = False
        if buildings is not None:
            if (
                not isinstance(buildings, (list, tuple))
                or not 1 <= len(buildings) <= MAX_SCOPE_BUILDINGS
            ):
                raise ClassroomError(
                    "invalid_scope",
                    f"buildings 必须是 1–{MAX_SCOPE_BUILDINGS} 个教学楼 id 的数组。",
                )
            cleaned: list[str] = []
            for item in buildings:
                if not isinstance(item, str):
                    raise ClassroomError("invalid_scope", "buildings 中的教学楼 id 必须是字符串。")
                value = item.strip()
                if not value:
                    raise ClassroomError("invalid_scope", "buildings 中的教学楼 id 不能为空。")
                if value == "全部":
                    if len(buildings) != 1:
                        raise ClassroomError(
                            "invalid_scope", "buildings 不能同时包含“全部”和其他教学楼。"
                        )
                    cleaned = ["全部"]
                    break
                if value not in cleaned:
                    cleaned.append(value)
            if not cleaned:
                raise ClassroomError("invalid_scope", "buildings 不能为空。")
            all_requested = cleaned == ["全部"]
            selected = None if all_requested else cleaned

        if current != "全部":
            if all_requested:
                raise ClassroomError(
                    "invalid_scope", "building 与 buildings=全部 指定了不同范围。"
                )
            if selected is not None and current not in selected:
                raise ClassroomError(
                    "invalid_scope", "building 与 buildings 指定了不同范围。"
                )
            if selected is None:
                selected = [current]

        if mode == "overview":
            return "overview", selected
        if mode == "full":
            return "full", selected
        return ("overview", None) if selected is None else ("full", selected)

    async def _fetch_scope(
        self, base: ClassroomQuery, selected: list[str] | None
    ) -> list[ClassroomDirectory]:
        if selected is None:
            directory = await self.client.fetch(base, gate=self._semaphore)
            self._validate(base, directory)
            return [directory]
        directories: list[ClassroomDirectory] = []
        for building in selected:
            query = ClassroomQuery.create(
                campus=base.campus,
                date=base.date.isoformat(),
                periods=base.periods,
                building=building,
            )
            directory = await self.client.fetch(query, gate=self._semaphore)
            self._validate(query, directory)
            directories.append(directory)
        return directories

    @staticmethod
    def _merge_directories(
        directories: list[ClassroomDirectory],
    ) -> ClassroomDirectory:
        rooms = tuple(room for directory in directories for room in directory.rooms)
        options = []
        seen: set[str] = set()
        for directory in directories:
            for option in directory.building_options:
                if option.id not in seen:
                    options.append(option)
                    seen.add(option.id)
        return ClassroomDirectory(rooms=rooms, building_options=tuple(options))

    async def _verify_rooms(
        self,
        query: ClassroomQuery,
        directory: ClassroomDirectory,
        *,
        verify_all: bool,
        deadline: float,
    ) -> tuple[ClassroomDirectory, ClassroomVerification]:
        """Verify rooms through the official detail endpoint.

        ``verify_all=False`` is the legacy candidate path: rooms already marked
        occupied by ``search-all`` are kept occupied.  ``verify_all=True`` is
        the full path: every room is checked, and a conflict stays uncertain.
        Both paths share this loop so they cannot drift.
        """
        codes = [
            room.code
            for room in directory.rooms
            if verify_all or not room.occupied
        ]
        search_all_occupied = sum(1 for room in directory.rooms if room.occupied)
        if not codes:
            return directory, ClassroomVerification(
                search_all_occupied=search_all_occupied,
                checked_all_rooms=verify_all,
            )

        lookups = await self.client.fetch_details(
            query,
            codes,
            concurrency=self.detail_concurrency,
            deadline=deadline,
            gate=self._semaphore,
        )
        selected = set(query.periods)
        rooms = []
        detail_checked = 0
        detail_occupied = 0
        detail_failed = 0
        unknown = 0
        conflicts = 0
        reserved_count = 0
        unqueried = sum(
            1
            for lookup in lookups.values()
            if lookup.error == "detail_skipped"
        )
        timed_out = sum(
            1
            for lookup in lookups.values()
            if lookup.error == "detail_timeout"
        )
        for room in directory.rooms:
            if not verify_all and room.occupied:
                rooms.append(room)
                continue
            lookup = lookups.get(room.code)
            if lookup is None or lookup.occupied_periods is None:
                detail_failed += 1
                if room.occupied:
                    # search-all already says occupied; keep the conservative
                    # answer rather than turning a known occupied room unknown.
                    rooms.append(room)
                else:
                    unknown += 1
                    rooms.append(
                        replace(
                            room,
                            occupied=False,
                            occupancy_source="unknown",
                            occupancy_unknown=True,
                            search_all_conflict=False,
                        )
                    )
                continue
            detail_checked += 1
            if selected & lookup.occupied_periods:
                detail_occupied += 1
                rooms.append(
                    replace(
                        room,
                        occupied=True,
                        occupancy_source="detail",
                        occupancy_unknown=False,
                        search_all_conflict=False,
                    )
                )
            elif room.occupied:
                # search-all says occupied, detail says free.  This is evidence
                # conflict, not a verified free room.  Keep it uncertain even
                # when the caller asks to include conflict candidates.
                conflicts += 1
                reserved = is_reserved_name(room.name)
                if reserved:
                    reserved_count += 1
                unknown += 1
                rooms.append(
                    replace(
                        room,
                        occupied=False,
                        occupancy_source="reserved" if reserved else "conflict",
                        occupancy_unknown=True,
                        search_all_conflict=True,
                    )
                )
            else:
                rooms.append(
                    replace(
                        room,
                        occupied=False,
                        occupancy_source="detail",
                        occupancy_unknown=False,
                        search_all_conflict=False,
                    )
                )

        verification = ClassroomVerification(
            search_all_occupied=search_all_occupied,
            detail_checked=detail_checked,
            detail_occupied=detail_occupied,
            detail_failed=detail_failed,
            unknown=unknown,
            conflicts=conflicts,
            reserved=reserved_count,
            unqueried=unqueried,
            timed_out=timed_out,
            checked_all_rooms=verify_all,
        )
        if detail_failed == len(codes):
            reason = "detail_unavailable"
            for code in codes:
                lookup = lookups.get(code)
                if lookup is not None and lookup.error:
                    reason = lookup.error
                    break
            raise ClassroomError(
                "availability_unverified",
                "无法逐间校验所选教室的占用状态；未返回空闲结论。",
                details={
                    "query": query.public(),
                    "reason": reason,
                    "detail_failed": detail_failed,
                    "unqueried": unqueried,
                    "rooms": len(codes),
                },
            )
        return (
            ClassroomDirectory(
                rooms=tuple(rooms),
                building_options=directory.building_options,
            ),
            verification,
        )

    # ------------------------------------------------------------------
    # Results
    # ------------------------------------------------------------------
    def _overview_result(
        self,
        query: ClassroomQuery,
        directory: ClassroomDirectory,
        selected: list[str] | None,
        *,
        only_available: bool,
        offset: int,
        limit: int,
    ) -> dict[str, Any]:
        options = directory.public_building_options()
        all_ids = [option["id"] for option in options if option["id"] != "全部"]
        if selected is not None:
            missing = [item for item in selected if item not in all_ids]
            if missing:
                raise ClassroomError(
                    "invalid_building",
                    "学校未返回所选教学楼；请使用 building_options 中的 id。",
                    details={
                        "query": query.public(),
                        "buildings": directory.public_buildings(),
                        "building_options": options,
                    },
                )
        wanted = set(selected) if selected is not None else set(all_ids)
        names = {option["id"]: option["name"] for option in options}
        grouped: dict[str, list] = {}
        for room in directory.rooms:
            grouped.setdefault(room.building, []).append(room)
        order = {building: index for index, building in enumerate(all_ids)}
        summaries = []
        for building, rooms in grouped.items():
            if building not in wanted:
                continue
            total = len(rooms)
            search_all_occupied = sum(1 for room in rooms if room.occupied)
            candidates = total - search_all_occupied
            summaries.append(
                {
                    "id": building,
                    "name": names.get(building, building),
                    "total": total,
                    "search_all_occupied": search_all_occupied,
                    "candidates": candidates,
                    "candidate_ratio": round(candidates / total, 3) if total else 0.0,
                    "requires_narrowing": candidates > 0,
                }
            )
        summaries.sort(key=lambda item: order.get(item["id"], 999))
        selected_ids = [item["id"] for item in summaries]
        total_rooms = sum(item["total"] for item in summaries)
        total_search = sum(item["search_all_occupied"] for item in summaries)
        total_candidates = sum(item["candidates"] for item in summaries)
        warnings = [
            "全部范围只返回按教学楼汇总，不返回具体房间；"
            "candidates 是 search-all 说空闲的数量，不是最终空闲数。"
        ]
        if selected is None:
            warnings.append(
                "请选择 1–2 栋教学楼后使用 mode=full 获取逐间校验后的完整空闲列表。"
            )
        return {
            "status": "overview",
            "source_system": "classrooms",
            "mode": "overview",
            "query": {
                **query.public(),
                "building": "全部",
                "buildings": selected or ["全部"],
                "only_available": only_available,
                "include_conflicts": False,
            },
            "fetched_at": now(),
            "from_cache": False,
            "stale": False,
            "buildings": directory.public_buildings(),
            "selected_buildings": selected_ids,
            "building_options": options,
            "building_summaries": summaries,
            "period_groups": {
                label: list(periods) for label, periods in PERIOD_GROUPS.items()
            },
            "counts": {
                "total": total_rooms,
                "search_all_occupied": total_search,
                "candidates": total_candidates,
            },
            "pagination": {
                "offset": offset,
                "limit": limit,
                "total": 0,
                "returned": 0,
                "has_more": False,
                "next_offset": None,
            },
            "verification": {
                "search_all_occupied": total_search,
                "detail_checked": 0,
                "detail_occupied": 0,
                "detail_failed": 0,
                "unknown": None,
                "conflicts": 0,
                "reserved": 0,
                "unqueried": 0,
                "timed_out": 0,
                "checked_all_rooms": False,
                "request_complete": False,
                "availability_certainty": "unverified",
                "complete": False,
            },
            "truncated": False,
            "availability": "unverified",
            "availability_certainty": "unverified",
            "reason": "overview_only",
            "rooms": [],
            "conflict_candidates": [],
            "request_budget": {
                "search_all_requests": 1,
                "detail_requested": 0,
                "detail_attempted": 0,
                "detail_unqueried": 0,
                "detail_timed_out": 0,
                "detail_not_completed": 0,
                "total_requested": 1,
                "candidates_not_verified": total_candidates,
                "max_detail_requests": MAX_DETAIL_REQUESTS,
                "max_scope_rooms": MAX_SCOPE_ROOMS,
                "max_total_requests": MAX_TOTAL_SCOPE_REQUESTS,
                "detail_concurrency": self.detail_concurrency,
                "detail_deadline_seconds": None,
                "search_all_timeout_seconds": SEARCH_ALL_TIMEOUT_SECONDS,
                "scope_timeout_seconds": SEARCH_ALL_TIMEOUT_SECONDS,
            },
            "evidence": {
                "source": "学校公开 search-all 记录",
                "meaning": "只反映学校记录，不是现场空位、预约或准入证明",
                "cross_check": "none (overview)",
                "period_13": "unsupported",
            },
            "next_action": "choose_buildings",
            "warnings": warnings,
            "limitations": [
                "概览模式不请求 detail；counts.candidates 只是 search-all 的候选数，"
                "不能当作最终空闲房间。",
                "search-all 的 d.js 对多节次是交集且不含“借用”；必须用 mode=full 逐间校验。",
                "请选择 1–2 栋教学楼以获得更完整的学校记录对照。",
                "detail+search-all 仅是学校记录，不是现场空位或准入证明；"
                "冲突、保留教室和未校验房间都不能当作可用。",
                "第 13 节未验证：search-all 返回全占用、detail 没有 c_13 数据；"
                "本工具直接返回 unsupported_period，不猜成 1–12。",
                "上午 1–5、下午 6–9、晚上 10–12 仅是学校页面的节次分组，"
                "本工具不推断具体钟点时间。",
            ],
            "error": None,
        }

    def _building_summaries(self, directory: ClassroomDirectory) -> list[dict[str, Any]]:
        names = {option.id: option.name for option in directory.building_options}
        order = {
            option.id: index
            for index, option in enumerate(directory.building_options)
            if option.id != "全部"
        }
        grouped: dict[str, list] = {}
        for room in directory.rooms:
            grouped.setdefault(room.building, []).append(room)
        summaries = []
        for building, rooms in grouped.items():
            available = sum(
                1 for room in rooms if not room.occupied and not room.occupancy_unknown
            )
            occupied = sum(1 for room in rooms if room.occupied)
            unknown = sum(1 for room in rooms if room.occupancy_unknown)
            conflicts = sum(1 for room in rooms if room.search_all_conflict)
            reserved = sum(
                1
                for room in rooms
                if room.search_all_conflict and is_reserved_name(room.name)
            )
            summaries.append(
                {
                    "id": building,
                    "name": names.get(building, building),
                    "total": len(rooms),
                    "available": available,
                    "occupied": occupied,
                    "unknown": unknown,
                    "conflicts": conflicts,
                    "reserved": reserved,
                }
            )
        summaries.sort(key=lambda item: order.get(item["id"], 999))
        return summaries

    def _request_budget(
        self,
        *,
        search_count: int,
        verification: ClassroomVerification,
        mode: str,
        deadline: float,
    ) -> dict[str, Any]:
        requested = verification.detail_checked + verification.detail_failed
        return {
            "mode": mode,
            "search_all_requests": search_count,
            "detail_requested": requested,
            "detail_attempted": requested - verification.unqueried,
            "detail_unqueried": verification.unqueried,
            "detail_timed_out": verification.timed_out,
            "detail_not_completed": verification.unqueried + verification.timed_out,
            "total_requested": search_count + requested,
            "candidates_not_verified": 0,
            "max_detail_requests": MAX_DETAIL_REQUESTS,
            "max_scope_rooms": MAX_SCOPE_ROOMS,
            "max_total_requests": MAX_TOTAL_SCOPE_REQUESTS,
            "detail_concurrency": self.detail_concurrency,
            "detail_deadline_seconds": deadline,
            "search_all_timeout_seconds": SEARCH_ALL_TIMEOUT_SECONDS,
            "scope_timeout_seconds": deadline
            + search_count * SEARCH_ALL_TIMEOUT_SECONDS,
        }

    def _result(
        self,
        query: ClassroomQuery,
        directory: ClassroomDirectory,
        verification: ClassroomVerification,
        *,
        only_available: bool,
        offset: int,
        limit: int,
        mode: str = "single",
        selected_buildings: list[str] | None = None,
        building_summaries: list[dict[str, Any]] | None = None,
        include_conflicts: bool = False,
        request_budget: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        query_building = query.building
        if (
            selected_buildings is not None
            and len(selected_buildings) == 1
            and selected_buildings[0] != "全部"
        ):
            query_building = selected_buildings[0]
        query_output = {
            **query.public(),
            "building": query_building,
            "only_available": only_available,
            "mode": mode,
            "include_conflicts": include_conflicts,
        }
        if selected_buildings is not None:
            query_output["buildings"] = selected_buildings
        buildings = directory.public_buildings()
        building_options = directory.public_building_options()

        rooms = list(directory.rooms)
        available_rooms = [
            room
            for room in rooms
            if not room.occupied
            and not room.occupancy_unknown
            and not room.search_all_conflict
        ]
        occupied_rooms = [room for room in rooms if room.occupied]
        unknown_rooms = [room for room in rooms if room.occupancy_unknown]
        conflict_rooms = [room for room in rooms if room.search_all_conflict]
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
            if unknown_rooms and not available_rooms:
                reason = "verification_incomplete"
                availability = "unknown"
            elif not available_rooms:
                reason = "all_occupied"
                availability = "all_occupied"
            else:
                reason = "no_matching_rooms"
                availability = "available"
        elif offset >= len(matched):
            status = "empty"
            reason = "offset_out_of_range"
            availability = (
                "available"
                if available_rooms
                else "unknown"
                if unknown_rooms
                else "all_occupied"
            )
        else:
            status = "ok"
            reason = None
            availability = (
                "available"
                if available_rooms
                else "unknown"
                if unknown_rooms
                else "all_occupied"
            )

        warnings: list[str] = []
        if unknown_rooms:
            warnings.append(
                f"{len(unknown_rooms)} 间教室存在冲突、保留标记或 detail 校验失败，"
                "已标记 uncertain，未计入 available。"
            )
        if verification.conflicts:
            warnings.append(
                f"{verification.conflicts} 间教室 search-all 标占用但 detail 未显示占用；"
                "这是证据冲突，不会升级为 available，已另列 conflict_candidates。"
            )
            if verification.reserved:
                warnings.append(
                    f"其中 {verification.reserved} 间名称含保留标记；"
                    "名称规则只是已知限制线索，不是完整准入识别。"
                )
        if include_conflicts:
            warnings.append(
                "include_conflicts 已弃用且不再改变可用性：冲突房间只作为"
                " conflict_candidates 展示，仍为 uncertain，only_available 不返回。"
            )

        limitations = [
            "available 仅表示 search-all 与 detail 的学校记录在所选节次上均未"
            "显示占用；它不是现场空位、预约或准入证明。",
            "search-all 的 d.js 对多节次表现为交集且不含“借用”；该交集结论"
            "只来自已测样本，不能证明学校服务端的一般实现。",
            "search-all 标占用但 detail 未显示占用的房间是证据冲突："
            "保留 occupancy_unknown=true，绝不升级为 available；"
            "冲突房间另列 conflict_candidates。",
            "include_conflicts 仅为兼容旧调用保留，已不再把冲突房间升级为可用。",
            "名称含研究生专用/教师专用/专用教室等标记只是保留线索，"
            "不是完整准入识别；这些房间保持 uncertain。",
            "detail 的 lessArr 表示课程/借用块覆盖的节次；"
            "本工具仍只输出所选节次的合并状态，不输出逐节矩阵。",
            "第 13 节未验证：search-all 返回全占用、detail 没有 c_13 数据；"
            "本工具返回 unsupported_period，不猜成 1–12。",
            "上午 1–5、下午 6–9、晚上 10–12 仅是学校页面的节次分组，"
            "本工具不推断具体钟点时间。",
            "capacity 来自学校返回的 num；缺失或非正数时为 null，未从其他来源推断。",
            "一次查询只返回本次请求的数据，不使用缓存、后台刷新或定时抓取。",
            "教室目录和占用是公开页面记录，不代表预约、占座或选课结果。",
        ]
        if has_more:
            limitations.append(
                "结果已按 limit 分页截断；counts.matched 是过滤后的总数，"
                "本次只返回其中一部分，请用 offset/next_offset 继续。"
            )

        if request_budget is None:
            requested = verification.detail_checked + verification.detail_failed
            request_budget = {
                "search_all_requests": 1,
                "detail_requested": requested,
                "detail_attempted": requested - verification.unqueried,
                "detail_unqueried": verification.unqueried,
                "detail_timed_out": verification.timed_out,
                "detail_not_completed": verification.unqueried + verification.timed_out,
                "total_requested": 1 + requested,
                "candidates_not_verified": 0,
                "max_detail_requests": MAX_DETAIL_REQUESTS,
                "max_scope_rooms": MAX_SCOPE_ROOMS,
                "max_total_requests": MAX_TOTAL_SCOPE_REQUESTS,
                "detail_concurrency": self.detail_concurrency,
                "detail_deadline_seconds": self.detail_deadline,
                "search_all_timeout_seconds": SEARCH_ALL_TIMEOUT_SECONDS,
                "scope_timeout_seconds": self.detail_deadline
                + SEARCH_ALL_TIMEOUT_SECONDS,
            }

        result = {
            "status": status,
            "source_system": "classrooms",
            "mode": mode,
            "query": query_output,
            "fetched_at": now(),
            "from_cache": False,
            "stale": False,
            "buildings": buildings,
            "selected_buildings": selected_buildings
            if selected_buildings is not None
            else [query.building],
            "building_options": building_options,
            "period_groups": {
                label: list(periods) for label, periods in PERIOD_GROUPS.items()
            },
            "counts": {
                "total": len(rooms),
                "available": len(available_rooms),
                "occupied": len(occupied_rooms),
                "unknown": len(unknown_rooms),
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
            "verification": verification.public(),
            "truncated": has_more,
            "availability": availability,
            "availability_certainty": verification.availability_certainty,
            "reason": reason,
            "rooms": [room.public() for room in page],
            "conflict_candidates": [
                room.public() for room in conflict_rooms
            ],
            "request_budget": request_budget,
            "evidence": {
                "source": "学校公开 search-all 与 per-room detail 记录",
                "meaning": "只反映学校记录，不是现场空位、预约或准入证明",
                "cross_check": "search-all + detail",
                "period_13": "unsupported",
            },
            "warnings": warnings,
            "limitations": limitations,
            "error": None,
        }
        if building_summaries is not None:
            result["building_summaries"] = building_summaries
        return result

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    @staticmethod
    def _validate(query: ClassroomQuery, directory: ClassroomDirectory) -> None:
        buildings = directory.public_buildings()
        building_options = directory.public_building_options()
        if query.building != "全部":
            if query.building not in buildings:
                raise ClassroomError(
                    "invalid_building",
                    "学校未返回该教学楼；请使用 building_options 中的 id 或“全部”。",
                    details={
                        "query": query.public(),
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
                    "query": query.public(),
                    "buildings": buildings,
                    "building_options": building_options,
                    "directory_empty": True,
                },
            )


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
