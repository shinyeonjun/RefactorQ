from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


from refactorq.core.candidate import Candidate
from refactorq.core.repo import RepoSnapshot

PlanMode = Literal["safe", "balanced", "report"]
ProposalAuthority = Literal["solver_proposal"]
OptimizerBackendName = Literal["greedy", "qubo_local_search"]
SelectionSource = Literal[
    "heuristic",
    "optimizer_greedy",
    "optimizer_qubo",
    "planner_override_of_optimizer",
    "optimizer_rejected_no_batch",
]
HardConstraintStatus = Literal["not_evaluated", "satisfied", "violated"]
ProposalRevalidationStatus = Literal["not_needed", "accepted", "overridden", "rejected"]


class PlanEdge(BaseModel):
    from_id: str = Field(alias="fromId")
    to_id: str = Field(alias="toId")
    kind: Literal["conflict", "dependency", "synergy"]
    reason: str


class ExcludedCandidate(BaseModel):
    candidate: Candidate
    reason: str


class ProposalRevalidation(BaseModel):
    status: ProposalRevalidationStatus = "not_needed"
    rejection_reasons: list[str] = Field(default_factory=list, alias="rejectionReasons")
    final_selected_candidate_ids: list[str] = Field(default_factory=list, alias="finalSelectedCandidateIds")


class BaselineComparison(BaseModel):
    heuristic_selected_candidate_ids: list[str] = Field(default_factory=list, alias="heuristicSelectedCandidateIds")
    optimizer_selected_candidate_ids: list[str] = Field(default_factory=list, alias="optimizerSelectedCandidateIds")


class SolverProposal(BaseModel):
    authority: ProposalAuthority = "solver_proposal"
    repo: RepoSnapshot
    adapter_names: list[str] = Field(default_factory=list, alias="adapterNames")
    candidates: list[Candidate] = Field(default_factory=list)
    backend: OptimizerBackendName | None = None
    selected_candidate_ids: list[str] = Field(default_factory=list, alias="selectedCandidateIds")
    objective_score: float | None = Field(default=None, alias="objectiveScore")
    hard_constraint_status: HardConstraintStatus = Field(default="not_evaluated", alias="hardConstraintStatus")
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class PlanResult(BaseModel):
    mode: PlanMode
    repo: RepoSnapshot
    adapter_names: list[str] = Field(default_factory=list, alias="adapterNames")
    selected_candidates: list[Candidate] = Field(default_factory=list, alias="selectedCandidates")
    excluded_candidates: list[ExcludedCandidate] = Field(default_factory=list, alias="excludedCandidates")
    edges: list[PlanEdge] = Field(default_factory=list)
    required_checks: list[str] = Field(default_factory=list, alias="requiredChecks")
    candidate_count: int = Field(default=0, alias="candidateCount")
    selected_count: int = Field(default=0, alias="selectedCount")
    excluded_count: int = Field(default=0, alias="excludedCount")
    selection_source: SelectionSource = Field(default="heuristic", alias="selectionSource")
    solver_proposal: SolverProposal | None = Field(default=None, alias="solverProposal")
    proposal_revalidation: ProposalRevalidation = Field(default_factory=ProposalRevalidation, alias="proposalRevalidation")
    baseline_comparison: BaselineComparison | None = Field(default=None, alias="baselineComparison")

    @model_validator(mode="after")
    def _validate_selection_invariants(self) -> "PlanResult":
        if self.selection_source == "optimizer_rejected_no_batch":
            if self.proposal_revalidation.status != "rejected":
                raise ValueError("optimizer_rejected_no_batch requires rejected proposal revalidation")
            if self.selected_candidates or self.proposal_revalidation.final_selected_candidate_ids:
                raise ValueError("optimizer_rejected_no_batch requires an empty authoritative final batch")
        if self.selection_source in {"optimizer_greedy", "optimizer_qubo"} and self.proposal_revalidation.status != "accepted":
            raise ValueError("accepted optimizer selection sources require accepted proposal revalidation")
        if self.selection_source == "planner_override_of_optimizer" and self.proposal_revalidation.status != "overridden":
            raise ValueError("planner_override_of_optimizer requires overridden proposal revalidation")
        return self