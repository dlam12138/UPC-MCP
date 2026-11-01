"""Unified CAS auto-auth for the three implemented authenticated systems."""

from .credentials import Binding, Credential, CredentialStore
from .errors import AutoAuthError

__all__ = [
    "AutoAuthError",
    "Binding",
    "Credential",
    "CredentialStore",
]
