from datetime import datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, SecretStr


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Session(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    revision: str = Field(default_factory=lambda: uuid4().hex)
    student_id: str = Field(pattern=r"^[0-9]{8,16}$", repr=False)
    account: str = Field(pattern=r"^[0-9]{1,24}$", repr=False)
    cookie: SecretStr
    headers: dict[str, str] = Field(repr=False)
    seed: dict[str, str] = Field(repr=False)
    acquired_at: str = Field(default_factory=now)
    verified_at: str | None = None
    state: Literal["stored", "unverified", "auth_expired"] = "stored"
    source: Literal["normal_cdp", "normal_cdp_recording"]

    def public(self) -> dict:
        return {
            "session_ref": "self",
            "state": self.state,
            "student_id_masked": self.student_id[:2]
            + "*" * (len(self.student_id) - 4)
            + self.student_id[-2:],
            "acquired_at": self.acquired_at,
            "last_verified_at": self.verified_at,
            "expires_at": None,
            "expiry_known": False,
        }
