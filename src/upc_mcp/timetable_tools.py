"""Independent MCP registration for the campus-app timetable.

The shared ``server.py`` calls ``register_timetable_tools(server, directory)``
after creating the existing server.  Three read-only tools are registered here;
status remains part of the existing
``upc_session_status(system="timetable")`` integration.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field

from .auto_auth.errors import AutoAuthError
from .timetable_protocol import TimetableError
from .timetable_service import TimetableService
from .timetable_store import TimetableStore

if TYPE_CHECKING:
    from .auto_auth.coordinator import AutoAuthCoordinator


def _result(payload: dict, error: bool = False) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
        structured_content=payload,
        is_error=error,
    )


def register_timetable_tools(
    server: MCPServer,
    directory: Path,
    *,
    service: TimetableService | None = None,
    auto_auth: AutoAuthCoordinator | None = None,
) -> TimetableService:
    """Register ``upc_timetable_terms``, ``upc_timetable_week`` and range.

    ``service`` is an optional injection seam for in-process tests.  Production
    callers pass the local state directory and unified auto-auth coordinator.
    """

    timetable_service = service or TimetableService(
        TimetableStore(directory), auto_auth=auto_auth
    )
    readonly = ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    )

    @server.tool(structured_output=False, annotations=readonly)
    async def upc_timetable_terms(
        session_ref: Literal["self"] = "self",
    ) -> CallToolResult:
        """Read the school's term list and current teaching week; no write."""

        try:
            return _result(await timetable_service.terms())
        except (TimetableError, AutoAuthError) as error:
            return _result(error.payload(), True)
        except Exception:
            return _result(
                TimetableError(
                    "local_error", "本地课表服务异常；未输出原始响应或凭据。"
                ).payload(),
                True,
            )

    @server.tool(structured_output=False, annotations=readonly)
    async def upc_timetable_week(
        year: Annotated[
            str,
            Field(
                min_length=1,
                max_length=32,
                description="学期列表中的学年，例如 2026-2027。",
            ),
        ],
        term: Annotated[
            str,
            Field(
                min_length=1,
                max_length=32,
                description="学期列表中的学期编号，例如 1。",
            ),
        ],
        week: Annotated[int, Field(ge=1, le=30, description="该学期内的教学周，1 起。")],
        session_ref: Literal["self"] = "self",
    ) -> CallToolResult:
        """Read one week's own courses; returns names/teachers/rooms by design."""

        try:
            return _result(await timetable_service.week(year, term, week))
        except (TimetableError, AutoAuthError) as error:
            return _result(error.payload(), True)
        except Exception:
            return _result(
                TimetableError(
                    "local_error", "本地课表服务异常；未输出原始响应或凭据。"
                ).payload(),
                True,
            )

    @server.tool(structured_output=False, annotations=readonly)
    async def upc_timetable_range(
        year: Annotated[
            str,
            Field(
                min_length=1,
                max_length=32,
                description="学期列表中的学年，例如 2026-2027。",
            ),
        ],
        term: Annotated[
            str,
            Field(
                min_length=1,
                max_length=32,
                description="学期列表中的学期编号，例如 1。",
            ),
        ],
        start_date: Annotated[
            str,
            Field(
                min_length=10,
                max_length=10,
                pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$",
                description="自然日期起点，显式 YYYY-MM-DD（Asia/Shanghai）。",
            ),
        ],
        end_date: Annotated[
            str,
            Field(
                min_length=10,
                max_length=10,
                pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$",
                description="自然日期终点，含当天；范围最多 14 天/3 个教学周。",
            ),
        ],
        session_ref: Literal["self"] = "self",
    ) -> CallToolResult:
        """Read one bounded natural-date range with every course instance.

        The range is capped at 14 days and three teaching weeks. Each item has a
        real date, ISO weekday, periods, time, location/teacher and a stable
        ``instance_id``; counts.total/returned/complete describe the whole
        range. Any required week failure fails the call instead of returning a
        partial empty timetable.
        """

        try:
            return _result(
                await timetable_service.range(
                    year, term, start_date, end_date
                )
            )
        except (TimetableError, AutoAuthError) as error:
            return _result(error.payload(), True)
        except Exception:
            return _result(
                TimetableError(
                    "local_error", "本地课表服务异常；未输出原始响应或凭据。"
                ).payload(),
                True,
            )

    return timetable_service
