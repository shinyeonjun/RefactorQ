from __future__ import annotations

from pathlib import Path
from typing import Iterable

from refactorq.core.candidate import Candidate
from refactorq.core.verification.service import candidate_verification_state

from .edges import batch_conflict_reason, synergy_bonus
from .models import ExcludedCandidate, PlanMode
from .scoring import candidate_diff_lines, candidate_score, is_high_risk, ranking_key


MODE_BATCH_LIMITS = {
    "safe": {"max_candidates": 12, "max_files": 8, "max_diff_lines": 180, "max_guarded": 0, "max_high_risk": 0},
    "balanced": {"max_candidates": 24, "max_files": 16, "max_diff_lines": 420, "max_guarded": 8, "max_high_risk": 2},
}


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
    limits = MODE_BATCH_LIMITS[mode]
    if len(selected) >= limits["max_candidates"]:
        return f"{mode} batch candidate budget reached"
    if candidate.apply_mode_hint == "guarded" and guarded_count >= limits["max_guarded"]:
        return f"{mode} guarded candidate budget reached"
    if is_high_risk(candidate) and high_risk_count >= limits["max_high_risk"]:
        return f"{mode} high-risk candidate budget reached"
    if diff_lines_used + candidate_diff_lines(candidate) > limits["max_diff_lines"]:
        return f"{mode} batch diff budget reached"
    if len(selected_files | set(candidate.files)) > limits["max_files"]:
        return f"{mode} batch file budget reached"
    if any(dependency_id not in selected_ids for dependency_id in candidate.dependencies):
        return "candidate dependencies are not satisfied in the current batch"
    for current in selected:
        reason = batch_conflict_reason(candidate, current)
        if reason is not None:
            return reason
    return None


def filter_candidates(candidates: Iterable[Candidate], mode: PlanMode) -> tuple[list[Candidate], list[ExcludedCandidate]]:
    filter_fn = {
        "safe": _safe_filter,
        "balanced": _balanced_filter,
        "report": _report_filter,
    }[mode]
    eligible: list[Candidate] = []
    excluded: list[ExcludedCandidate] = []
    for candidate in sorted(candidates, key=ranking_key):
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
            return candidate_score(candidate) + synergy_bonus(candidate, selected)

        best = sorted(feasible, key=lambda candidate: (-selection_score(candidate), ranking_key(candidate)))[0]
        pending.remove(best)
        selected.append(best)
        selected_ids.add(best.id)
        selected_files.update(best.files)
        diff_lines_used += candidate_diff_lines(best)
        if best.apply_mode_hint == "guarded":
            guarded_count += 1
        if is_high_risk(best):
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


def planner_revalidate_candidates(
    root: Path,
    candidates: Iterable[Candidate],
    mode: PlanMode,
) -> tuple[list[Candidate], list[ExcludedCandidate]]:
    selected, excluded = filter_candidates(candidates, mode)
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


def optimizer_candidate_pool(candidates: Iterable[Candidate], mode: PlanMode) -> list[Candidate]:
    filter_fn = {
        "safe": _safe_filter,
        "balanced": _balanced_filter,
        "report": _report_filter,
    }[mode]
    eligible: list[Candidate] = []
    for candidate in sorted(candidates, key=ranking_key):
        if filter_fn(candidate) is None:
            eligible.append(candidate)
    return eligible
