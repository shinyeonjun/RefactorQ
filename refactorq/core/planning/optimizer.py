from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, Field

from refactorq.core.candidate import Candidate
from refactorq.core.planning.models import OptimizerBackendName, PlanMode, SolverProposal
from refactorq.core.repo import RepoSnapshot



class OptimizerBudget(BaseModel):
    mode_budget: int = Field(alias="modeBudget")
    max_files: int = Field(alias="maxFiles")



class OptimizerCandidateInput(BaseModel):
    candidate: Candidate
    base_score: float = Field(alias="baseScore")
    diff_lines: int = Field(alias="diffLines")
    files: list[str]
    guarded: bool = False
    high_risk: bool = Field(default=False, alias="highRisk")
    conflict_ids: list[str] = Field(default_factory=list, alias="conflictIds")


class OptimizerProblem(BaseModel):
    repo: RepoSnapshot
    adapter_names: list[str] = Field(default_factory=list, alias="adapterNames")
    mode: PlanMode
    budget: OptimizerBudget
    candidates: list[OptimizerCandidateInput] = Field(default_factory=list)


class SelectionBackend(Protocol):
    name: str

    def solve(self, problem: OptimizerProblem) -> SolverProposal: ...


class GreedySelectionBackend:
    name: OptimizerBackendName = "greedy"

    def solve(self, problem: OptimizerProblem) -> SolverProposal:
        ordered = sorted(problem.candidates, key=lambda item: (-item.base_score, item.candidate.id))
        selected: list[OptimizerCandidateInput] = []
        for candidate in ordered:
            if _is_feasible(problem, selected, candidate):
                selected.append(candidate)
        return _proposal(problem, self.name, selected)


class QuboLocalSearchSolver:
    name: OptimizerBackendName = "qubo_local_search"

    def solve(self, problem: OptimizerProblem) -> SolverProposal:
        greedy = GreedySelectionBackend().solve(problem)
        by_id = {item.candidate.id: item for item in problem.candidates}
        selected = [by_id[candidate_id] for candidate_id in greedy.selected_candidate_ids if candidate_id in by_id]
        best = list(selected)
        best_score = _objective(best)
        improved = True
        while improved:
            improved = False
            remaining = [item for item in problem.candidates if item.candidate.id not in {chosen.candidate.id for chosen in best}]
            for candidate in remaining:
                if not _is_feasible(problem, best, candidate):
                    continue
                trial = [*best, candidate]
                score = _objective(trial)
                if score > best_score:
                    best = trial
                    best_score = score
                    improved = True
                    break
            if improved:
                continue
            for index, current in enumerate(best):
                reduced = [item for idx, item in enumerate(best) if idx != index]
                for candidate in remaining:
                    if not _is_feasible(problem, reduced, candidate):
                        continue
                    trial = [*reduced, candidate]
                    score = _objective(trial)
                    if score > best_score:
                        best = trial
                        best_score = score
                        improved = True
                        break
                if improved:
                    break
        return _proposal(problem, self.name, best)


def _proposal(problem: OptimizerProblem, backend: OptimizerBackendName, selected: list[OptimizerCandidateInput]) -> SolverProposal:
    selected_ids = [item.candidate.id for item in selected]
    return SolverProposal(
        repo=problem.repo,
        adapterNames=problem.adapter_names,
        candidates=[item.candidate for item in problem.candidates],
        backend=backend,
        selectedCandidateIds=selected_ids,
        objectiveScore=_objective(selected),
        hardConstraintStatus="satisfied",
        diagnostics={
            "selectedCount": len(selected),
            "selectedFiles": sorted({path for item in selected for path in item.files}),
            "budget": problem.budget.model_dump(by_alias=True),
        },
    )


def _objective(selected: list[OptimizerCandidateInput]) -> float:
    score = sum(item.base_score for item in selected)
    score -= 0.001 * sum(item.diff_lines for item in selected)
    return score


def _is_feasible(
    problem: OptimizerProblem,
    selected: list[OptimizerCandidateInput],
    candidate: OptimizerCandidateInput,
) -> bool:
    budget = problem.budget
    if len(selected) >= budget.mode_budget:
        return False
    selected_files = {path for item in selected for path in item.files}
    if len(selected_files | set(candidate.files)) > budget.max_files:
        return False
    selected_ids = {item.candidate.id for item in selected}
    if any(conflict_id in selected_ids for conflict_id in candidate.conflict_ids):
        return False
    return True
