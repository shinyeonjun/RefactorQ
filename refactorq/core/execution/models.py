from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from refactorq.core.candidate import Candidate
from refactorq.core.planning import PlanMode, PlanResult
from refactorq.core.repo import RepoSnapshot
from refactorq.core.verification import VerificationResult

ApplyStatus = Literal["applied", "no_changes"]
RunStatus = Literal["passed", "rolled_back", "no_changes"]
RepairStatus = Literal["not_needed", "repaired", "failed", "skipped"]


class ExecutionCandidateNote(BaseModel):
    candidate: Candidate
    reason: str


class GitExecutionResult(BaseModel):
    enabled: bool
    available: bool
    clean: bool
    base_branch: str | None = Field(default=None, alias="baseBranch")
    execution_branch: str | None = Field(default=None, alias="executionBranch")
    commit_sha: str | None = Field(default=None, alias="commitSha")
    reason: str | None = None


class RepairResult(BaseModel):
    status: RepairStatus
    attempted: bool
    touched_files: list[str] = Field(default_factory=list, alias="touchedFiles")
    reason: str | None = None


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
    supported_auto_candidates: int = Field(alias="supportedAutoCandidates")
    supported_guarded_candidates: int = Field(alias="supportedGuardedCandidates")
    unsupported_candidates: int = Field(alias="unsupportedCandidates")
    applied_candidate_kinds: list[str] = Field(default_factory=list, alias="appliedCandidateKinds")
    git_branching_supported: bool = Field(alias="gitBranchingSupported")
    git_reason: str | None = Field(default=None, alias="gitReason")


class BoundaryExecutionSummary(BaseModel):
    cross_language_candidates: int = Field(alias="crossLanguageCandidates")
    boundary_sensitive_candidates: int = Field(alias="boundarySensitiveCandidates")
    blocked_boundary_candidates: int = Field(alias="blockedBoundaryCandidates")
    contract_artifacts: list[str] = Field(default_factory=list, alias="contractArtifacts")
    highest_impact: str = Field(default="none", alias="highestImpact")


class ReportResult(BaseModel):
    mode: PlanMode
    repo: RepoSnapshot
    plan: PlanResult
    execution_support: ExecutionSupportSummary = Field(alias="executionSupport")
    boundary_execution: BoundaryExecutionSummary = Field(alias="boundaryExecution")


class RunResult(BaseModel):
    mode: PlanMode
    repo: RepoSnapshot
    plan: PlanResult
    apply: ApplyResult
    verification: VerificationResult
    status: RunStatus
    rollback_applied: bool = Field(alias="rollbackApplied")
    repair: RepairResult
    git: GitExecutionResult
