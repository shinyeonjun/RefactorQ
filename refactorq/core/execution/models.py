from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from refactorq.core.candidate import Candidate
from refactorq.core.planning import PlanMode, PlanResult
from refactorq.core.repo import RepoSnapshot
from refactorq.core.verification import ProofStatus, VerificationResult


ApplyStatus = Literal["applied", "no_changes", "rejected_no_batch"]
RunStatus = Literal["passed", "rolled_back", "no_changes", "rejected_no_batch"]
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
    source_kind: str = Field(default="local", alias="sourceKind")
    working_root: str | None = Field(default=None, alias="workingRoot")
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
    ready_boundary_candidates: int = Field(default=0, alias="readyBoundaryCandidates")
    contract_ready_candidates: int = Field(default=0, alias="contractReadyCandidates")
    contract_blocked_candidates: int = Field(default=0, alias="contractBlockedCandidates")
    contract_artifacts: list[str] = Field(default_factory=list, alias="contractArtifacts")
    blocked_reasons: list[str] = Field(default_factory=list, alias="blockedReasons")
    highest_impact: str = Field(default="none", alias="highestImpact")
    proof_status: ProofStatus = Field(default="not_applicable", alias="proofStatus")
    missing_predicates: list[str] = Field(default_factory=list, alias="missingPredicates")
    proof_refs: list[str] = Field(default_factory=list, alias="proofRefs")



class BoundaryVerificationCandidateSummary(BaseModel):
    candidate_id: str = Field(alias="candidateId")
    kind: str
    required_checks: list[str] = Field(default_factory=list, alias="requiredChecks")
    available_checks: list[str] = Field(default_factory=list, alias="availableChecks")
    missing_required_checks: list[str] = Field(default_factory=list, alias="missingRequiredChecks")
    contract_artifacts: list[str] = Field(default_factory=list, alias="contractArtifacts")
    producer_side: list[str] = Field(default_factory=list, alias="producerSide")
    consumer_side: list[str] = Field(default_factory=list, alias="consumerSide")
    ready: bool
    blocked_reasons: list[str] = Field(default_factory=list, alias="blockedReasons")
    proof_status: ProofStatus = Field(default="not_applicable", alias="proofStatus")
    missing_predicates: list[str] = Field(default_factory=list, alias="missingPredicates")
    proof_refs: list[str] = Field(default_factory=list, alias="proofRefs")



class VerificationPlanSummary(BaseModel):
    required_checks: list[str] = Field(default_factory=list, alias="requiredChecks")
    available_checks: list[str] = Field(default_factory=list, alias="availableChecks")
    missing_required_checks: list[str] = Field(default_factory=list, alias="missingRequiredChecks")
    boundary_candidates: list[BoundaryVerificationCandidateSummary] = Field(default_factory=list, alias="boundaryCandidates")
    proof_status: ProofStatus = Field(default="not_applicable", alias="proofStatus")
    missing_predicates: list[str] = Field(default_factory=list, alias="missingPredicates")
    proof_refs: list[str] = Field(default_factory=list, alias="proofRefs")


class ReportResult(BaseModel):
    mode: PlanMode
    repo: RepoSnapshot
    plan: PlanResult
    execution_support: ExecutionSupportSummary = Field(alias="executionSupport")
    boundary_execution: BoundaryExecutionSummary = Field(alias="boundaryExecution")
    verification_plan: VerificationPlanSummary = Field(alias="verificationPlan")


class RunResult(BaseModel):
    mode: PlanMode
    repo: RepoSnapshot
    plan: PlanResult
    source_kind: str = Field(default="local", alias="sourceKind")
    working_root: str | None = Field(default=None, alias="workingRoot")
    apply: ApplyResult
    verification: VerificationResult
    status: RunStatus
    executed_selection_source: str = Field(default="heuristic", alias="executedSelectionSource")
    rollback_applied: bool = Field(alias="rollbackApplied")
    repair: RepairResult
    git: GitExecutionResult
