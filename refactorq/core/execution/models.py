from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from refactorq.core.candidate import Candidate
from refactorq.core.planning import PlanMode, PlanResult
from refactorq.core.repo import RepoSnapshot
from refactorq.core.verification import VerificationResult

ApplyStatus = Literal["applied", "no_changes"]
RunStatus = Literal["passed", "rolled_back", "no_changes"]


class ExecutionCandidateNote(BaseModel):
    candidate: Candidate
    reason: str


class ApplyResult(BaseModel):
    mode: PlanMode
    repo: RepoSnapshot
    plan: PlanResult
    status: ApplyStatus
    applied_candidates: list[Candidate] = Field(default_factory=list, alias="appliedCandidates")
    skipped_candidates: list[ExecutionCandidateNote] = Field(default_factory=list, alias="skippedCandidates")
    changed_files: list[str] = Field(default_factory=list, alias="changedFiles")


class ExecutionSupportSummary(BaseModel):
    supported_candidates: int = Field(alias="supportedCandidates")
    unsupported_candidates: int = Field(alias="unsupportedCandidates")
    applied_candidate_kinds: list[str] = Field(default_factory=list, alias="appliedCandidateKinds")


class ReportResult(BaseModel):
    mode: PlanMode
    repo: RepoSnapshot
    plan: PlanResult
    execution_support: ExecutionSupportSummary = Field(alias="executionSupport")


class RunResult(BaseModel):
    mode: PlanMode
    repo: RepoSnapshot
    plan: PlanResult
    apply: ApplyResult
    verification: VerificationResult
    status: RunStatus
    rollback_applied: bool = Field(alias="rollbackApplied")
