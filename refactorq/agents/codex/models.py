from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

GuardedApplyStatus = Literal["applied", "no_change", "unsupported"]


class GuardedApplyResult(BaseModel):
    status: GuardedApplyStatus
    touched_files: list[str] = Field(default_factory=list, alias="touchedFiles")
    summary: list[str] = Field(default_factory=list)
    details: dict[str, str] = Field(default_factory=dict)


__all__ = ["GuardedApplyResult", "GuardedApplyStatus"]
