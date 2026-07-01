from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from refactorq.core.candidate import Candidate
from refactorq.core.candidate.models import AnchorRegion, VerificationCheck
from refactorq.core.verification import VerificationResult


GuardedApplyStatus = Literal["applied", "no_change", "unsupported"]
BoundedPatchMode = Literal["apply", "repair"]


class BoundedPatchScope(BaseModel):
    candidate_ids: list[str] = Field(default_factory=list, alias="candidateIds")
    allowed_files: list[str] = Field(default_factory=list, alias="allowedFiles")
    anchor_regions: list[AnchorRegion] = Field(default_factory=list, alias="anchorRegions")
    required_checks: list[VerificationCheck] = Field(default_factory=list, alias="requiredChecks")


class GuardedApplyRequest(BaseModel):
    mode: BoundedPatchMode = "apply"
    scope: BoundedPatchScope
    candidate: Candidate


class GuardedRepairRequest(BaseModel):
    mode: BoundedPatchMode = "repair"
    scope: BoundedPatchScope
    candidates: list[Candidate] = Field(default_factory=list)
    verification: VerificationResult


class GuardedApplyResult(BaseModel):
    status: GuardedApplyStatus
    candidate_ids: list[str] = Field(default_factory=list, alias="candidateIds")
    touched_files: list[str] = Field(default_factory=list, alias="touchedFiles")
    summary: list[str] = Field(default_factory=list)
    details: dict[str, str] = Field(default_factory=dict)


__all__ = [
    "BoundedPatchMode",
    "BoundedPatchScope",
    "GuardedApplyRequest",
    "GuardedApplyResult",
    "GuardedApplyStatus",
    "GuardedRepairRequest",
]
