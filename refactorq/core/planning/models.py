from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from refactorq.core.candidate import Candidate
from refactorq.core.repo import RepoSnapshot

PlanMode = Literal["safe", "balanced", "report"]


class PlanEdge(BaseModel):
    from_id: str = Field(alias="fromId")
    to_id: str = Field(alias="toId")
    kind: Literal["conflict", "dependency"]
    reason: str


class ExcludedCandidate(BaseModel):
    candidate: Candidate
    reason: str


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
