"""Per-system official service providers."""

from .library import LibraryProvider
from .service_portal import ServicePortalProvider
from .shiguang import ShiguangProvider
from .timetable import TimetableProvider

__all__ = [
    "LibraryProvider",
    "ServicePortalProvider",
    "ShiguangProvider",
    "TimetableProvider",
]
