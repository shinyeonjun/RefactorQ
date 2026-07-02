from __future__ import annotations

from pathlib import Path
from typing import Iterable

from refactorq.core.candidate import Candidate
from refactorq.core.repo import RepoSnapshot

from .edges import pairwise_conflict_reasons, plan_edges
from .models import BaselineComparison, ExcludedCandidate, PlanMode, PlanResult, ProposalRevalidation, SelectionSource, SolverProposal
from .optimizer import GreedySelectionBackend, OptimizerBudget, OptimizerCandidateInput, OptimizerProblem, QuboLocalSearchSolver
from .selection import MODE_BATCH_LIMITS, filter_candidates, optimizer_candidate_pool, planner_revalidate_candidates
from .scoring import candidate_diff_lines, candidate_score, is_high_risk, ranking_key


def normalize_solver_proposal(
    *,
    repo: RepoSnapshot,
    adapter_names: Iterable[str],
    candidates: Iterable[Candidate],
) -> SolverProposal:
    normalized_candidates: list[Candidate] = []
    seen_candidate_ids: set[str] = set()
    for candidate in candidates:
        if candidate.id in seen_candidate_ids:
            continue
        seen_candidate_ids.add(candidate.id)
        normalized_candidates.append(candidate)
    normalized_adapter_names = list(dict.fromkeys(adapter_names))
    return SolverProposal(repo=repo, adapterNames=normalized_adapter_names, candidates=normalized_candidates)



def _optimizer_budget(mode: PlanMode) -> OptimizerBudget:
    limits = MODE_BATCH_LIMITS.get(mode, MODE_BATCH_LIMITS["balanced"])
    return OptimizerBudget(
        modeBudget=limits["max_candidates"],
        maxFiles=limits["max_files"],
    )



def build_optimizer_problem(
    *,
    mode: PlanMode,
    repo: RepoSnapshot,
    adapter_names: list[str],
    candidates: list[Candidate],
) -> OptimizerProblem:
    conflict_ids: dict[str, set[str]] = {candidate.id: set(candidate.conflicts) for candidate in candidates}
    for index, left in enumerate(candidates):
        for right in candidates[index + 1 :]:
            if pairwise_conflict_reasons(left, right):
                conflict_ids[left.id].add(right.id)
                conflict_ids[right.id].add(left.id)

    optimizer_candidates = [
        OptimizerCandidateInput(
            candidate=candidate,
            baseScore=candidate_score(candidate),
            diffLines=candidate_diff_lines(candidate),
            files=list(candidate.files),
            guarded=candidate.apply_mode_hint == "guarded",
            highRisk=is_high_risk(candidate),
            conflictIds=sorted(conflict_ids[candidate.id]),
        )
        for candidate in candidates
    ]
    return OptimizerProblem(
        repo=repo,
        adapterNames=adapter_names,
        mode=mode,
        budget=_optimizer_budget(mode),
        candidates=optimizer_candidates,
    )



def _optimizer_backend_for(mode: PlanMode) -> GreedySelectionBackend | QuboLocalSearchSolver:
    return QuboLocalSearchSolver() if mode in {"balanced", "report"} else GreedySelectionBackend()


def _required_checks(candidates: list[Candidate]) -> list[str]:
    ordered: dict[str, None] = {}
    for candidate in candidates:
        for check in candidate.required_checks:
            ordered.setdefault(check, None)
    return list(ordered)



def _selection_source_from_backend(backend: str | None) -> SelectionSource:

    if backend == "greedy":
        return "optimizer_greedy"
    if backend == "qubo_local_search":
        return "optimizer_qubo"
    return "heuristic"



def _merge_authoritative_exclusions(
    all_candidates: list[Candidate],
    initial_excluded: list[ExcludedCandidate],
    *,
    proposed_ids: set[str],
    selected_ids: set[str],
    revalidated_excluded: list[ExcludedCandidate],
) -> list[ExcludedCandidate]:
    merged: dict[str, ExcludedCandidate] = {item.candidate.id: item for item in initial_excluded}
    for item in revalidated_excluded:
        merged[item.candidate.id] = item
    for candidate in all_candidates:
        if candidate.id in selected_ids or candidate.id in merged:
            continue
        if candidate.id in proposed_ids:
            merged[candidate.id] = ExcludedCandidate(
                candidate=candidate,
                reason="planner revalidation removed optimizer candidate from the authoritative batch",
            )
            continue
        merged[candidate.id] = ExcludedCandidate(
            candidate=candidate,
            reason="optimizer proposal did not select candidate",
        )
    return sorted(merged.values(), key=lambda item: ranking_key(item.candidate))


def _same_candidate_ids(left: list[str], right: list[str]) -> bool:
    return set(left) == set(right) and len(left) == len(right)


def _apply_solver_proposal(
    *,
    root: Path,
    proposal: SolverProposal,
    solver_proposal: SolverProposal,
    initial_excluded: list[ExcludedCandidate],
    mode: PlanMode,
) -> tuple[list[Candidate], list[ExcludedCandidate], SelectionSource, ProposalRevalidation]:
    proposed_by_id = {candidate.id: candidate for candidate in proposal.candidates}
    proposed_candidates = [
        proposed_by_id[candidate_id]
        for candidate_id in solver_proposal.selected_candidate_ids
        if candidate_id in proposed_by_id
    ]
    revalidation_mode: PlanMode = "balanced" if mode == "report" else mode
    revalidated_selected, revalidated_excluded = planner_revalidate_candidates(
        root,
        proposed_candidates,
        revalidation_mode,
    )
    final_ids = [candidate.id for candidate in revalidated_selected]
    proposed_ids = set(solver_proposal.selected_candidate_ids)
    source = _selection_source_from_backend(solver_proposal.backend)

    if not solver_proposal.selected_candidate_ids:
        return (
            [],
            _merge_authoritative_exclusions(
                proposal.candidates,
                initial_excluded,
                proposed_ids=set(),
                selected_ids=set(),
                revalidated_excluded=[],
            ),
            source,
            ProposalRevalidation(status="accepted", finalSelectedCandidateIds=[]),
        )

    selected_ids = set(final_ids)
    excluded = _merge_authoritative_exclusions(
        proposal.candidates,
        initial_excluded,
        proposed_ids=proposed_ids,
        selected_ids=selected_ids,
        revalidated_excluded=revalidated_excluded,
    )
    if _same_candidate_ids(final_ids, solver_proposal.selected_candidate_ids):
        return (
            revalidated_selected,
            excluded,
            source,
            ProposalRevalidation(status="accepted", finalSelectedCandidateIds=final_ids),
        )
    if final_ids:
        return (
            revalidated_selected,
            excluded,
            "planner_override_of_optimizer",
            ProposalRevalidation(
                status="overridden",
                rejectionReasons=[item.reason for item in revalidated_excluded],
                finalSelectedCandidateIds=final_ids,
            ),
        )
    return (
        [],
        excluded,
        "optimizer_rejected_no_batch",
        ProposalRevalidation(
            status="rejected",
            rejectionReasons=[item.reason for item in revalidated_excluded]
            or ["planner revalidation rejected the optimizer proposal"],
            finalSelectedCandidateIds=[],
        ),
    )


def build_plan(*, mode: PlanMode, repo: RepoSnapshot, adapter_names: list[str], candidates: list[Candidate]) -> PlanResult:
    proposal = normalize_solver_proposal(repo=repo, adapter_names=adapter_names, candidates=candidates)
    root = Path(proposal.repo.root)
    heuristic_selected, _ = filter_candidates(proposal.candidates, mode)
    selected, excluded = planner_revalidate_candidates(root, proposal.candidates, mode)
    selection_source: SelectionSource = "heuristic"
    proposal_revalidation = ProposalRevalidation()

    optimizer_pool = optimizer_candidate_pool(proposal.candidates, mode)
    solver_proposal = None
    baseline_comparison = None
    if optimizer_pool:
        optimizer_problem = build_optimizer_problem(
            mode=mode,
            repo=proposal.repo,
            adapter_names=proposal.adapter_names,
            candidates=optimizer_pool,
        )
        solver_proposal = _optimizer_backend_for(mode).solve(optimizer_problem)
        baseline_comparison = BaselineComparison(
            heuristicSelectedCandidateIds=[candidate.id for candidate in heuristic_selected],
            optimizerSelectedCandidateIds=list(solver_proposal.selected_candidate_ids),
        )
        selected, excluded, selection_source, proposal_revalidation = _apply_solver_proposal(
            root=root,
            proposal=proposal,
            solver_proposal=solver_proposal,
            initial_excluded=excluded,
            mode=mode,
        )

    edge_candidates = [*selected, *[item.candidate for item in excluded]]
    return PlanResult(
        mode=mode,
        repo=proposal.repo,
        adapterNames=proposal.adapter_names,
        selectedCandidates=selected,
        excludedCandidates=excluded,
        edges=plan_edges(edge_candidates),
        requiredChecks=_required_checks(selected),
        candidateCount=len(proposal.candidates),
        selectedCount=len(selected),
        excludedCount=len(excluded),
        selectionSource=selection_source,
        solverProposal=solver_proposal,
        proposalRevalidation=proposal_revalidation,
        baselineComparison=baseline_comparison,
    )
