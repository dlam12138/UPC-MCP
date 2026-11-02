"""Local validation, concurrency, filtering, and pagination for classrooms.

The protocol/HTTP adapter is :mod:`upc_mcp.classrooms`.  This service module
contains no credentials and no school-specific storage; it turns validated
public responses into bounded, explicit MCP results.

Two scopes are exposed:

* ``overview`` — one ``search-all`` for a campus, directory counts only;
* ``full`` — one or two buildings (or every building) verified room by room
  through the official ``detail`` endpoint.  The legacy ``query`` entry point
  uses the same per-room detail path; there is no search-all candidate path.

``search-all`` only supplies the room directory and building options.  Its
``d.js`` occupancy is never used for a decision, so a room whose detail record
shows no selected-period occupancy is available even when ``d.js`` marks it.
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
    # Legacy single-scope query (shared per-room verification)
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
        diagnostic: dict[str, Any] = {}
        for directory in directories:
            if directory.search_all_diagnostic:
                diagnostic = directory.search_all_diagnostic
                break
        return ClassroomDirectory(
            rooms=rooms,
            building_options=tuple(options),
            search_all_diagnostic=diagnostic,
        )

    async def _verify_rooms(
        self,
        query: ClassroomQuery,
        directory: ClassroomDirectory,
        *,
        deadline: float,
    ) -> tuple[ClassroomDirectory, ClassroomVerification]:
        """Verify every room in the selected scope through official detail.

        ``search-all`` is used only for the directory.  Every directory room is
        requested, including rooms that ``d.js`` marked occupied.  A detail
        success determines period occupancy by intersecting the selected
        periods with the union of all non-empty blocks' ``lessArr`` for the
        matching date.  A detail failure is unknown; it never falls back to
        ``d.js`` occupancy.  If every planned detail request fails, the query
        raises ``availability_unverified``.
        """

        codes = [room.code for room in directory.rooms]
        search_all_occupied = int(
            directory.search_all_diagnostic.get("occupied_code_count") or 0
        )
        restricted_count = sum(1 for room in directory.rooms if room.access_restriction)
        if not codes:
            return directory, ClassroomVerification(
                search_all_occupied=search_all_occupied,
                restricted=restricted_count,
                checked_all_rooms=True,
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
            lookup = lookups.get(room.code)
            if lookup is None or lookup.occupied_periods is None:
                detail_failed += 1
                unknown += 1
                rooms.append(
                    replace(
                        room,
                        occupancy="unknown",
                        occupancy_source="unknown",
                    )
                )
                continue
            detail_checked += 1
            if selected & lookup.occupied_periods:
                detail_occupied += 1
                rooms.append(
                    replace(
                        room,
                        occupancy="occupied",
                        occupancy_source="detail",
                    )
                )
            else:
                rooms.append(
                    replace(
                        room,
                        occupancy="available",
                        occupancy_source="detail",
                    )
                )

        verification = ClassroomVerification(
            search_all_occupied=search_all_occupied,
            detail_checked=detail_checked,
            detail_occupied=detail_occupied,
            detail_failed=detail_failed,
            unknown=unknown,
            restricted=restricted_count,
            unqueried=unqueried,
            timed_out=timed_out,
            checked_all_rooms=True,
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
                search_all_diagnostic=directory.search_all_diagnostic,
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
            summaries.append(
                {
                    "id": building,
                    "name": names.get(building, building),
                    "total": total,
                    "pending_detail": total,
                    "requires_detail": total > 0,
                }
            )
        summaries.sort(key=lambda item: order.get(item["id"], 999))
        selected_ids = [item["id"] for item in summaries]
        total_rooms = sum(item["total"] for item in summaries)
        warnings = [
            "overview 只返回学校教室目录的楼宇和教室数，不判断任何房间占用。"
        ]
        if selected is None:
            warnings.append(
                "请选择 1–2 栋教学楼后使用 mode=full 获取逐间 detail 校验后的记录。"
            )
        else:
            warnings.append(
                "已按所选楼宇过滤目录；当前仍是 overview，未请求 detail。"
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
                "pending_detail": total_rooms,
                "detail_checked": 0,
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
                "search_all_occupied": 0,
                "search_all_occupied_used": False,
                "detail_checked": 0,
                "detail_occupied": 0,
                "detail_failed": 0,
                "unknown": None,
                "conflicts": 0,
                "reserved": 0,
                "restricted": 0,
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
            "restricted_rooms": [],
            "conflict_candidates": [],
            "search_all_diagnostic": directory.search_all_diagnostic,
            "request_budget": {
                "search_all_requests": 1,
                "detail_requested": 0,
                "detail_attempted": 0,
                "detail_unqueried": 0,
                "detail_timed_out": 0,
                "detail_not_completed": 0,
                "total_requested": 1,
                "candidates_not_verified": 0,
                "max_detail_requests": MAX_DETAIL_REQUESTS,
                "max_scope_rooms": MAX_SCOPE_ROOMS,
                "max_total_requests": MAX_TOTAL_SCOPE_REQUESTS,
                "detail_concurrency": self.detail_concurrency,
                "detail_deadline_seconds": None,
                "search_all_timeout_seconds": SEARCH_ALL_TIMEOUT_SECONDS,
                "scope_timeout_seconds": SEARCH_ALL_TIMEOUT_SECONDS,
            },
            "evidence": {
                "source": "学校公开 search-all 教室目录",
                "meaning": "只说明目录楼宇和教室数，不判断占用、可用或准入",
                "cross_check": "none (overview; detail not requested)",
                "period_13": "unsupported",
            },
            "next_action": "choose_buildings",
            "warnings": warnings,
            "limitations": [
                "概览模式不请求 detail，不返回任何房间级可用性；"
                "counts.pending_detail 只是尚待逐间校验的目录数。",
                "search-all 的 d.js 不参与占用判定，概览也不导出候选空闲数或比例。",
                "请选择 1–2 栋教学楼以获得逐间 detail 记录。",
                "detail 失败、超时或结构异常的房间为 unknown，不当作空闲或占用。",
                "名称含研究生专用/教师专用/专用教室等标记只是名称线索，"
                "不证明准入；普通自习推荐与占用记录分开。",
                "第 13 节未验证：本工具直接返回 unsupported_period，不猜成 1–12。",
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
            available = sum(1 for room in rooms if room.occupancy == "available")
            ordinary_available = sum(
                1 for room in rooms if room.ordinary_self_study
            )
            restricted_available = sum(
                1
                for room in rooms
                if room.occupancy == "available" and room.access_restriction
            )
            occupied = sum(1 for room in rooms if room.occupancy == "occupied")
            unknown = sum(1 for room in rooms if room.occupancy == "unknown")
            restricted = sum(1 for room in rooms if room.access_restriction)
            summaries.append(
                {
                    "id": building,
                    "name": names.get(building, building),
                    "total": len(rooms),
                    "available": available,
                    "ordinary_available": ordinary_available,
                    "restricted_available": restricted_available,
                    "occupied": occupied,
                    "unknown": unknown,
                    "restricted": restricted,
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
            room for room in rooms if room.occupancy == "available"
        ]
        ordinary_rooms = [room for room in rooms if room.ordinary_self_study]
        restricted_rooms = [
            room for room in rooms if room.access_restriction
        ]
        restricted_available = [
            room
            for room in available_rooms
            if room.access_restriction
        ]
        occupied_rooms = [
            room for room in rooms if room.occupancy == "occupied"
        ]
        unknown_rooms = [room for room in rooms if room.occupancy == "unknown"]
        matched = ordinary_rooms if only_available else rooms
        page = matched[offset : offset + limit]
        has_more = offset + len(page) < len(matched)
        next_offset = offset + len(page) if has_more else None

        if not rooms:
            status = "empty"
            reason = "no_rooms_for_building"
            availability = "no_rooms"
        elif not matched:
            status = "empty"
            if not available_rooms and unknown_rooms:
                reason = "verification_incomplete"
                availability = "unknown"
            elif not available_rooms:
                reason = "all_occupied"
                availability = "all_occupied"
            elif only_available and not ordinary_rooms:
                reason = "restricted_only"
                availability = "restricted_only"
            else:
                reason = "no_matching_rooms"
                availability = "available"
        elif offset >= len(matched):
            status = "empty"
            reason = "offset_out_of_range"
            availability = (
                "available"
                if ordinary_rooms
                else "restricted_only"
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
                if ordinary_rooms
                else "restricted_only"
                if available_rooms
                else "unknown"
                if unknown_rooms
                else "all_occupied"
            )

        warnings: list[str] = []
        if unknown_rooms:
            warnings.append(
                f"{len(unknown_rooms)} 间教室的 detail 校验失败、日期缺失或结构异常，"
                "occupancy=unknown，未计入 available。"
            )
        if restricted_rooms:
            warnings.append(
                f"{len(restricted_rooms)} 间教室名称含使用限制线索，已单列 "
                "restricted_rooms；仅名称线索，不证明准入。"
            )
        if restricted_available:
            warnings.append(
                f"其中 {len(restricted_available)} 间 detail 记录在所选节次无占用；"
                "它们仍不是普通自习推荐，不计入 only_available。"
            )
        if include_conflicts:
            warnings.append(
                "include_conflicts 已弃用且恒无冲突；d.js 不参与占用判定，"
                "conflict_candidates 恒为空。"
            )

        limitations = [
            "available 只表示 detail 记录在所选节次没有非空 lessArr 交集；"
            "它不是现场空位、预约或准入证明。",
            "available_restricted 表示 detail 记录空闲但名称含使用限制线索；"
            "它计入 available，但 ordinary_self_study=false，"
            "only_available 默认不返回，另见 restricted_rooms。",
            "search-all 只提供教室目录和楼宇选项；d.js 缺失、畸形或与 detail "
            "矛盾都不影响占用判定，也不构成冲突。",
            "detail 失败、超时、日期缺失或结构异常的房间 occupancy=unknown，"
            "不当作空闲，也不回退为 d.js 占用；全部房间都失败时返回 "
            "availability_unverified。",
            "detail 的 lessArr 表示课程/借用块覆盖的节次；本工具汇总当天全部"
            "非空块后只输出所选节次的合并状态，不输出逐节矩阵。",
            "名称含研究生专用/教师专用/专用教室等标记只是名称线索，"
            "不是完整准入识别，也不改变占用记录。",
            "restricted_rooms 是所选范围全部受限房间，"
            "不随 only_available 或房间分页变化。",
            "第 13 节未验证：本工具返回 unsupported_period，不猜成 1–12。",
            "上午 1–5、下午 6–9、晚上 10–12 仅是学校页面的节次分组，"
            "本工具不推断具体钟点时间。",
            "capacity 来自学校返回的 num；缺失或非正数时为 null，未从其他来源推断。",
            "一次查询只返回本次请求的数据，不使用缓存、后台刷新或定时抓取。",
            "教室目录和 detail 是公开页面记录，不代表预约、占座或选课结果。",
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
                "ordinary_available": len(ordinary_rooms),
                "restricted_available": len(restricted_available),
                "occupied": len(occupied_rooms),
                "unknown": len(unknown_rooms),
                "restricted": len(restricted_rooms),
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
            "restricted_rooms": [room.public() for room in restricted_rooms],
            "conflict_candidates": [],
            "search_all_diagnostic": directory.search_all_diagnostic,
            "request_budget": request_budget,
            "evidence": {
                "source": "学校公开 search-all 教室目录与 per-room detail 记录",
                "meaning": "只反映学校记录，不是现场空位、预约或准入证明",
                "cross_check": "per-room detail; search-all directory only",
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
