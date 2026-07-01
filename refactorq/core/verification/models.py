from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

VerificationStatus = Literal["passed", "failed", "skipped"]
VerificationKind = Literal["parse", "typecheck", "lint", "build", "unit_test"]


class VerificationCheckResult(BaseModel):
    name: str
    kind: VerificationKind
    status: VerificationStatus
    evidence: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)


class VerificationResult(BaseModel):
    status: Literal["passed", "failed"]
    checks: list[VerificationCheckResult] = Field(default_factory=list)


__all__ = ["VerificationCheckResult", "VerificationResult", "VerificationKind", "VerificationStatus"]
