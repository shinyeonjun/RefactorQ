from __future__ import annotations

from pathlib import Path
from typing import Iterable

from refactorq.core.candidate import Candidate
from refactorq.core.repo import RepoSnapshot
from refactorq.core.verification.service import candidate_verification_state

from .models import BaselineComparison, ExcludedCandidate, PlanEdge, PlanMode, PlanResult, ProposalRevalidation, SelectionSource, SolverProposal
from .optimizer import GreedySelectionBackend, OptimizerBudget, OptimizerCandidateInput, OptimizerProblem, QuboLocalSearchSolver


_APPLY_MODE_PRIORITY = {"auto": 0, "guarded": 1, "report_only": 2}
_IMPACT_PRIORITY = {"none": 0, "low": 1, "medium": 2, "high": 3}
_MODE_BATCH_LIMITS = {
    "safe": {"max_candidates": 12, "max_files": 8, "max_diff_lines": 180, "max_guarded": 0, "max_high_risk": 0},
    "balanced": {"max_candidates": 24, "max_files": 16, "max_diff_lines": 420, "max_guarded": 8, "max_high_risk": 2},
}


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



def _first_file(candidate: Candidate) -> str:
    return candidate.files[0] if candidate.files else ""


def _benefit_tuple(candidate: Candidate) -> tuple[float, float, float, float, float]:
    benefit = candidate.estimated_benefit
    return (
        -benefit.cycle_reduction,
        -benefit.complexity_reduction,
        -benefit.duplication_reduction,
        -benefit.maintainability_gain,
        -benefit.perf_gain,
    )

def _ranking_key(candidate: Candidate) -> tuple[object, ...]:
    risk = candidate.estimated_risk
    diff = candidate.estimated_diff
    return (
        -_candidate_score(candidate),
        _APPLY_MODE_PRIORITY[candidate.apply_mode_hint],
        _IMPACT_PRIORITY[candidate.boundary_impact.impact_level],
        risk.semantic_risk,
        risk.api_risk,
        risk.runtime_risk,
        risk.conflict_risk,
        -candidate.confidence,
        *_benefit_tuple(candidate),
        diff.files_touched,
        diff.lines_modified,
        diff.lines_added,
        diff.lines_deleted,
        _first_file(candidate),
        candidate.id,
    )


def _has_required_checks(candidate: Candidate) -> bool:
    return bool(candidate.required_checks)


def _is_boundary_changing(candidate: Candidate) -> bool:
    return candidate.boundary_impact.impact_level == "high"


def _is_cross_language(candidate: Candidate) -> bool:
    return candidate.boundary_impact.cross_language

def _is_unsupported_worker_guess(candidate: Candidate) -> bool:
    if candidate.language != "typescript":
        return False
    return any(detector.startswith("typescript-bridge") for detector in candidate.provenance.detectors)


def _is_contract_preserving_cross_language_candidate(candidate: Candidate) -> bool:
    return (
        candidate.kind in {"extract_function", "inline_function", "duplicate_logic", "remove_abstraction"}
        and len(candidate.files) == 1
        and candidate.scope in {"local", "module"}
    )


def _safe_filter(candidate: Candidate) -> str | None:
    if candidate.apply_mode_hint != "auto":
        return "requires guarded or report-only handling"
    if _is_cross_language(candidate):
        return "cross-language boundary candidates are excluded in safe mode"
    if candidate.boundary_impact.impact_level not in {"none", "low"}:
        return "boundary-changing candidates are excluded in safe mode"
    if not _has_required_checks(candidate):
        return "candidate is missing required checks"
    return None


def _balanced_filter(candidate: Candidate) -> str | None:
    if candidate.apply_mode_hint == "report_only":
        return "report-only candidate retained as explanatory exclusion"
    if _is_boundary_changing(candidate):
        return "boundary-changing candidate requires stronger verification than balanced mode baseline"
    if _is_cross_language(candidate):
        if not candidate.boundary_impact.contract_artifacts:
            return "cross-language candidate requires explicit boundary contract artifacts before balanced execution"
        if candidate.boundary_impact.impact_level != "low":
            return "cross-language candidate requires lower boundary impact before balanced execution"
        if candidate.apply_mode_hint == "guarded" and not _is_contract_preserving_cross_language_candidate(candidate):
            return "guarded cross-language candidate retained as report until guarded boundary execution is stronger"
        if candidate.apply_mode_hint not in {"auto", "guarded"}:
            return "cross-language candidate is not execution-ready in balanced mode"
        if not _has_required_checks(candidate):
            return "cross-language candidate is missing required checks"
    if _is_unsupported_worker_guess(candidate):
        return "unsupported TypeScript bridge guess excluded until worker-backed semantics are available"
    return None


def _report_filter(candidate: Candidate) -> str | None:
    return None


def _candidate_diff_lines(candidate: Candidate) -> int:
    diff = candidate.estimated_diff
    return diff.lines_modified + diff.lines_added + diff.lines_deleted


def _candidate_score(candidate: Candidate) -> float:
    benefit = candidate.estimated_benefit
    risk = candidate.estimated_risk
    diff = candidate.estimated_diff
    verification_burden = len(candidate.required_checks)
    return (
        2.5 * benefit.cycle_reduction
        + 2.0 * benefit.complexity_reduction
        + 2.0 * benefit.duplication_reduction
        + 1.5 * benefit.maintainability_gain
        + 1.0 * benefit.perf_gain
        + 0.8 * candidate.confidence
        - 2.1 * risk.semantic_risk
        - 1.6 * risk.api_risk
        - 1.2 * risk.runtime_risk
        - 1.0 * risk.conflict_risk
        - 0.6 * risk.test_risk
        - 0.03 * diff.files_touched
        - 0.001 * _candidate_diff_lines(candidate)
        - 0.05 * _IMPACT_PRIORITY[candidate.boundary_impact.impact_level]
        - 0.04 * verification_burden
    )


def _is_high_risk(candidate: Candidate) -> bool:
    risk = candidate.estimated_risk
    return (
        risk.semantic_risk >= 0.4
        or risk.api_risk >= 0.25
        or risk.runtime_risk >= 0.3
        or candidate.boundary_impact.impact_level in {"medium", "high"}
    )


def _batch_selection_reason(
    candidate: Candidate,
    *,
    mode: PlanMode,
    selected: list[Candidate],
    selected_ids: set[str],
    selected_files: set[str],
    diff_lines_used: int,
    guarded_count: int,
    high_risk_count: int,
) -> str | None:
    if mode == "report":
        return None
    limits = _MODE_BATCH_LIMITS[mode]
    if len(selected) >= limits["max_candidates"]:
        return f'{mode} batch candidate budget reached'
    if candidate.apply_mode_hint == "guarded" and guarded_count >= limits["max_guarded"]:
        return f'{mode} guarded candidate budget reached'
    if _is_high_risk(candidate) and high_risk_count >= limits["max_high_risk"]:
        return f'{mode} high-risk candidate budget reached'
    if diff_lines_used + _candidate_diff_lines(candidate) > limits["max_diff_lines"]:
        return f'{mode} batch diff budget reached'
    if len(selected_files | set(candidate.files)) > limits["max_files"]:
        return f'{mode} batch file budget reached'
    if any(dependency_id not in selected_ids for dependency_id in candidate.dependencies):
        return 'candidate dependencies are not satisfied in the current batch'
    for current in selected:
        reason = _batch_conflict_reason(candidate, current)
        if reason is not None:
            return reason
    return None


def _filter_candidates(candidates: Iterable[Candidate], mode: PlanMode) -> tuple[list[Candidate], list[ExcludedCandidate]]:
    filter_fn = {
        "safe": _safe_filter,
        "balanced": _balanced_filter,
        "report": _report_filter,
    }[mode]
    eligible: list[Candidate] = []
    excluded: list[ExcludedCandidate] = []
    for candidate in sorted(candidates, key=_ranking_key):
        reason = filter_fn(candidate)
        if reason is None:
            eligible.append(candidate)
            continue
        excluded.append(ExcludedCandidate(candidate=candidate, reason=reason))

    if mode == "report":
        return eligible, excluded

    pending = list(eligible)
    selected: list[Candidate] = []
    selected_ids: set[str] = set()
    selected_files: set[str] = set()
    diff_lines_used = 0
    guarded_count = 0
    high_risk_count = 0

    while pending:
        feasible: list[Candidate] = []
        for candidate in pending:
            reason = _batch_selection_reason(
                candidate,
                mode=mode,
                selected=selected,
                selected_ids=selected_ids,
                selected_files=selected_files,
                diff_lines_used=diff_lines_used,
                guarded_count=guarded_count,
                high_risk_count=high_risk_count,
            )
            if reason is None:
                feasible.append(candidate)

        if not feasible:
            break

        def selection_score(candidate: Candidate) -> float:
            synergy_bonus = 0.0
            for current in selected:
                if _is_duplicate_extract_synergy(candidate, current):
                    synergy_bonus += 0.18
                if _is_duplicate_remove_abstraction_synergy(candidate, current):
                    synergy_bonus += 0.16
                if _is_cycle_split_synergy(candidate, current):
                    synergy_bonus += 0.14
                if _is_layer_move_synergy(candidate, current):
                    synergy_bonus += 0.15
            return _candidate_score(candidate) + synergy_bonus

        best = sorted(feasible, key=lambda candidate: (-selection_score(candidate), _ranking_key(candidate)))[0]
        pending.remove(best)
        selected.append(best)
        selected_ids.add(best.id)
        selected_files.update(best.files)
        diff_lines_used += _candidate_diff_lines(best)
        if best.apply_mode_hint == "guarded":
            guarded_count += 1
        if _is_high_risk(best):
            high_risk_count += 1

    for candidate in pending:
        reason = _batch_selection_reason(
            candidate,
            mode=mode,
            selected=selected,
            selected_ids=selected_ids,
            selected_files=selected_files,
            diff_lines_used=diff_lines_used,
            guarded_count=guarded_count,
            high_risk_count=high_risk_count,
        )
        excluded.append(ExcludedCandidate(candidate=candidate, reason=reason or f"{mode} batch could not place candidate"))
    return selected, excluded


def _planner_revalidate_candidates(root: Path, candidates: Iterable[Candidate], mode: PlanMode) -> tuple[list[Candidate], list[ExcludedCandidate]]:
    selected, excluded = _filter_candidates(candidates, mode)
    authoritative: list[Candidate] = []
    readiness_excluded: list[ExcludedCandidate] = []
    for candidate in selected:
        state = candidate_verification_state(root, candidate)
        if bool(state["ready"]):
            authoritative.append(candidate)
            continue
        blocked_reasons = [str(reason) for reason in state.get("blockedReasons", [])]
        missing_predicates = [str(predicate) for predicate in state.get("missingPredicates", [])]
        reason = blocked_reasons[0] if blocked_reasons else (
            missing_predicates[0]
            if missing_predicates
            else "planner revalidation rejected candidate due to verification readiness"
        )
        readiness_excluded.append(ExcludedCandidate(candidate=candidate, reason=reason))
    return authoritative, [*excluded, *readiness_excluded]



def _regions_overlap(left: Candidate, right: Candidate) -> bool:
    for left_region in left.anchor_regions:
        for right_region in right.anchor_regions:
            if left_region.file != right_region.file:
                continue
            if left_region.start_line <= right_region.end_line and right_region.start_line <= left_region.end_line:
                return True
    return False


def _shared_symbol_scope(left: Candidate, right: Candidate) -> bool:
    if left.language != right.language or left.scope != right.scope:
        return False
    return bool(set(left.symbols) & set(right.symbols))


def _same_file_non_local(left: Candidate, right: Candidate) -> bool:
    if left.scope == "local" and right.scope == "local":
        return False
    return bool(set(left.files) & set(right.files))


def _batch_conflict_reason(candidate: Candidate, current: Candidate) -> str | None:
    if _regions_overlap(candidate, current):
        return f'candidate overlaps already selected batch candidate {current.id}'
    if current.id in candidate.conflicts or candidate.id in current.conflicts:
        return f'candidate explicitly conflicts with already selected batch candidate {current.id}'
    if _shared_symbol_scope(candidate, current):
        return f'candidate shares the same symbol and scope as already selected batch candidate {current.id}'
    if _same_file_non_local(candidate, current):
        return f'candidate touches the same file as non-local already selected batch candidate {current.id}'
    return None


def _pairwise_conflict_reasons(left: Candidate, right: Candidate) -> list[str]:
    reasons: list[str] = []
    if _regions_overlap(left, right):
        reasons.append("overlapping anchor regions in the same file")
    elif _shared_symbol_scope(left, right):
        reasons.append("same symbol in the same language scope")
    elif _same_file_non_local(left, right):
        reasons.append("same file touched with at least one non-local candidate")
    if right.id in left.conflicts or left.id in right.conflicts:
        reasons.append("explicit conflict declared by candidate")
    return reasons


def _conflict_edge(left: Candidate, right: Candidate, reason: str) -> PlanEdge:
    first_id, second_id = sorted((left.id, right.id))
    return PlanEdge(fromId=first_id, toId=second_id, kind="conflict", reason=reason)



def _conflict_edges(candidates: list[Candidate]) -> list[PlanEdge]:
    edges: list[PlanEdge] = []
    seen: set[tuple[str, str, str]] = set()

    def add_edge(left: Candidate, right: Candidate, reason: str) -> None:
        edge = _conflict_edge(left, right, reason)
        key = (edge.from_id, edge.to_id, edge.reason)
        if key not in seen:
            seen.add(key)
            edges.append(edge)

    for index, left in enumerate(candidates):
        for right in candidates[index + 1 :]:
            for reason in _pairwise_conflict_reasons(left, right):
                add_edge(left, right, reason)
    return edges


def _optimizer_budget(mode: PlanMode) -> OptimizerBudget:
    limits = _MODE_BATCH_LIMITS.get(mode, _MODE_BATCH_LIMITS["balanced"])
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
            if _pairwise_conflict_reasons(left, right):
                conflict_ids[left.id].add(right.id)
                conflict_ids[right.id].add(left.id)

    optimizer_candidates = [
        OptimizerCandidateInput(
            candidate=candidate,
            baseScore=_candidate_score(candidate),
            diffLines=_candidate_diff_lines(candidate),
            files=list(candidate.files),
            guarded=candidate.apply_mode_hint == "guarded",
            highRisk=_is_high_risk(candidate),
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


def _is_duplicate_extract_dependency(left: Candidate, right: Candidate) -> bool:
    return (
        left.kind == "duplicate_logic"
        and right.kind == "extract_function"
        and bool(set(left.files) & set(right.files))
        and bool(set(left.symbols) & set(right.symbols))
    )


def _is_cycle_split_dependency(left: Candidate, right: Candidate) -> bool:
    return (
        left.kind == "split_large_module"
        and right.kind == "reduce_cycle"
        and bool(set(left.files) & set(right.files))
    )


def _is_move_symbol_layer_dependency(left: Candidate, right: Candidate) -> bool:
    return (
        left.kind == "move_symbol"
        and right.kind == "layer_violation_fix"
        and bool(set(left.files) & set(right.files))
        and bool(set(left.symbols) & set(right.symbols))
    )


def _is_boundary_review_dependency(left: Candidate, right: Candidate) -> bool:
    return (
        left.boundary_impact.cross_language
        and right.kind == "custom"
        and right.id.startswith("boundary-review-")
        and bool(set(left.boundary_impact.contract_artifacts) & set(right.files))
    )


def _dependency_edge(from_candidate: Candidate, to_candidate: Candidate, reason: str) -> PlanEdge:
    return PlanEdge(fromId=from_candidate.id, toId=to_candidate.id, kind="dependency", reason=reason)


def _synergy_edge(left: Candidate, right: Candidate, reason: str) -> PlanEdge:
    first_id, second_id = sorted((left.id, right.id))
    return PlanEdge(fromId=first_id, toId=second_id, kind="synergy", reason=reason)


def _is_duplicate_extract_synergy(left: Candidate, right: Candidate) -> bool:
    kinds = {left.kind, right.kind}
    return kinds == {"duplicate_logic", "extract_function"} and bool(set(left.files) & set(right.files))


def _is_duplicate_remove_abstraction_synergy(left: Candidate, right: Candidate) -> bool:
    kinds = {left.kind, right.kind}
    return kinds == {"duplicate_logic", "remove_abstraction"} and bool(set(left.files) & set(right.files))


def _is_cycle_split_synergy(left: Candidate, right: Candidate) -> bool:
    kinds = {left.kind, right.kind}
    return kinds == {"reduce_cycle", "split_large_module"} and bool(set(left.files) & set(right.files))



def _is_layer_move_synergy(left: Candidate, right: Candidate) -> bool:
    kinds = {left.kind, right.kind}
    return (
        kinds == {"layer_violation_fix", "move_symbol"}
        and bool(set(left.files) & set(right.files))
        and bool(set(left.symbols) & set(right.symbols))
    )
def _synergy_edges(candidates: list[Candidate]) -> list[PlanEdge]:
    edges: list[PlanEdge] = []
    seen: set[tuple[str, str, str]] = set()

    def add_edge(left: Candidate, right: Candidate, reason: str) -> None:
        edge = _synergy_edge(left, right, reason)
        key = (edge.from_id, edge.to_id, edge.reason)
        if key not in seen:
            seen.add(key)
            edges.append(edge)

    for index, left in enumerate(candidates):
        for right in candidates[index + 1 :]:
            if _is_duplicate_extract_synergy(left, right):
                add_edge(left, right, "duplicate consolidation and extraction reinforce the same file refactor")
            if _is_duplicate_remove_abstraction_synergy(left, right):
                add_edge(left, right, "duplicate cleanup pairs with removing thin wrappers in the same file")
            if _is_cycle_split_synergy(left, right):
                add_edge(left, right, "cycle reduction and module splitting reinforce the same structural cleanup")
            if _is_layer_move_synergy(left, right):
                add_edge(left, right, "layer boundary review and symbol relocation target the same cross-layer import")
    return edges


def _dependency_edges(candidates: list[Candidate]) -> list[PlanEdge]:
    by_id = {candidate.id: candidate for candidate in candidates}
    edges: list[PlanEdge] = []
    seen: set[tuple[str, str, str]] = set()

    def add_edge(from_candidate: Candidate, to_candidate: Candidate, reason: str) -> None:
        edge = _dependency_edge(from_candidate, to_candidate, reason)
        key = (edge.from_id, edge.to_id, edge.reason)
        if key not in seen:
            seen.add(key)
            edges.append(edge)

    for candidate in candidates:
        for dependency_id in candidate.dependencies:
            if dependency_id in by_id:
                add_edge(candidate, by_id[dependency_id], "explicit dependency declared by candidate")
        for other in candidates:
            if candidate.id == other.id:
                continue
            if _is_duplicate_extract_dependency(candidate, other):
                add_edge(candidate, other, "extract function before duplicate consolidation in the same file")
            if _is_cycle_split_dependency(candidate, other):
                add_edge(candidate, other, "reduce cycle before splitting the related module")
            if _is_move_symbol_layer_dependency(candidate, other):
                add_edge(candidate, other, "review layer violation before moving the shared boundary symbol")
            if _is_boundary_review_dependency(candidate, other):
                add_edge(candidate, other, "review boundary contract artifact before cross-language execution")
    return edges


def _required_checks(candidates: list[Candidate]) -> list[str]:
    ordered: dict[str, None] = {}
    for candidate in candidates:
        for check in candidate.required_checks:
            ordered.setdefault(check, None)
    return list(ordered)



def _optimizer_candidate_pool(candidates: Iterable[Candidate], mode: PlanMode) -> list[Candidate]:
    filter_fn = {
        "safe": _safe_filter,
        "balanced": _balanced_filter,
        "report": _report_filter,
    }[mode]
    eligible: list[Candidate] = []
    for candidate in sorted(candidates, key=_ranking_key):
        if filter_fn(candidate) is None:
            eligible.append(candidate)
    return eligible


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
            merged[item_id := candidate.id] = ExcludedCandidate(
                candidate=candidate,
                reason="planner revalidation removed optimizer candidate from the authoritative batch",
            )
            continue
        merged[candidate.id] = ExcludedCandidate(
            candidate=candidate,
            reason="optimizer proposal did not select candidate",
        )
    return sorted(merged.values(), key=lambda item: _ranking_key(item.candidate))

def build_plan(*, mode: PlanMode, repo: RepoSnapshot, adapter_names: list[str], candidates: list[Candidate]) -> PlanResult:
    proposal = normalize_solver_proposal(repo=repo, adapter_names=adapter_names, candidates=candidates)
    root = Path(proposal.repo.root)
    heuristic_selected, _ = _filter_candidates(proposal.candidates, mode)
    selected, excluded = _planner_revalidate_candidates(root, proposal.candidates, mode)
    selection_source: SelectionSource = "heuristic"
    proposal_revalidation = ProposalRevalidation()

    optimizer_pool = _optimizer_candidate_pool(proposal.candidates, mode)
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
        proposed_by_id = {candidate.id: candidate for candidate in proposal.candidates}
        proposed_candidates = [
            proposed_by_id[candidate_id]
            for candidate_id in solver_proposal.selected_candidate_ids
            if candidate_id in proposed_by_id
        ]
        revalidation_mode: PlanMode = "balanced" if mode == "report" else mode
        revalidated_selected, revalidated_excluded = _planner_revalidate_candidates(root, proposed_candidates, revalidation_mode)
        final_ids = [candidate.id for candidate in revalidated_selected]
        proposed_ids = set(solver_proposal.selected_candidate_ids)
        selected_ids = set(final_ids)

        if not solver_proposal.selected_candidate_ids:
            selected = []
            excluded = _merge_authoritative_exclusions(
                proposal.candidates,
                excluded,
                proposed_ids=set(),
                selected_ids=set(),
                revalidated_excluded=[],
            )
            selection_source = _selection_source_from_backend(solver_proposal.backend)
            proposal_revalidation = ProposalRevalidation(
                status="accepted",
                finalSelectedCandidateIds=[],
            )
        elif set(final_ids) == set(solver_proposal.selected_candidate_ids) and len(final_ids) == len(solver_proposal.selected_candidate_ids):
            selected = revalidated_selected
            excluded = _merge_authoritative_exclusions(
                proposal.candidates,
                excluded,
                proposed_ids=proposed_ids,
                selected_ids=selected_ids,
                revalidated_excluded=revalidated_excluded,
            )
            selection_source = _selection_source_from_backend(solver_proposal.backend)
            proposal_revalidation = ProposalRevalidation(
                status="accepted",
                finalSelectedCandidateIds=final_ids,
            )
        elif final_ids:
            selected = revalidated_selected
            excluded = _merge_authoritative_exclusions(
                proposal.candidates,
                excluded,
                proposed_ids=proposed_ids,
                selected_ids=selected_ids,
                revalidated_excluded=revalidated_excluded,
            )
            selection_source = "planner_override_of_optimizer"
            proposal_revalidation = ProposalRevalidation(
                status="overridden",
                rejectionReasons=[item.reason for item in revalidated_excluded],
                finalSelectedCandidateIds=final_ids,
            )
        else:
            selected = []
            excluded = _merge_authoritative_exclusions(
                proposal.candidates,
                excluded,
                proposed_ids=proposed_ids,
                selected_ids=set(),
                revalidated_excluded=revalidated_excluded,
            )
            selection_source = "optimizer_rejected_no_batch"
            proposal_revalidation = ProposalRevalidation(
                status="rejected",
                rejectionReasons=[item.reason for item in revalidated_excluded] or ["planner revalidation rejected the optimizer proposal"],
                finalSelectedCandidateIds=[],
            )

    edge_candidates = [*selected, *[item.candidate for item in excluded]]
    edges = _conflict_edges(edge_candidates)
    edges.extend(_dependency_edges(edge_candidates))
    edges.extend(_synergy_edges(edge_candidates))
    ordered_edges = sorted(edges, key=lambda edge: (edge.kind, edge.from_id, edge.to_id, edge.reason))

    return PlanResult(
        mode=mode,
        repo=proposal.repo,
        adapterNames=proposal.adapter_names,
        selectedCandidates=selected,
        excludedCandidates=excluded,
        edges=ordered_edges,
        requiredChecks=_required_checks(selected),
        candidateCount=len(proposal.candidates),
        selectedCount=len(selected),
        excludedCount=len(excluded),
        selectionSource=selection_source,
        solverProposal=solver_proposal,
        proposalRevalidation=proposal_revalidation,
        baselineComparison=baseline_comparison,
    )
