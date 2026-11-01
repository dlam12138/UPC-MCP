"""Provider protocol shared by the unified auto-auth coordinator."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ..cas import CasTicket
from ..credentials import Binding


@dataclass
class PreparedService:
    """Service-side state created before the CAS ticket (library JSESSIONID)."""

    cas_url: str | None = None
    cookies: list[dict] = field(default_factory=list)


class SystemProvider(Protocol):
    system: str

    def prepare(self) -> PreparedService:
        ...

    def establish(
        self, ticket: CasTicket, binding: Binding, prepared: PreparedService
    ) -> Any:
        ...
