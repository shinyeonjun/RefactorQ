from __future__ import annotations

from typing import Iterable

from refactorq.core.candidate import Candidate
from refactorq.core.repo import RepoSnapshot


from .models import ExcludedCandidate, PlanEdge, PlanMode, PlanResult

_APPLY_MODE_PRIORITY = {"auto": 0, "guarded": 1, "report_only": 2}
_IMPACT_PRIORITY = {"none": 0, "low": 1, "medium": 2, "high": 3}



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
    if _is_cross_language(candidate):
        return "cross-language candidate retained as report until boundary-aware execution lands"
    if _is_boundary_changing(candidate):
        return "boundary-changing candidate requires stronger verification than balanced mode baseline"
    if _is_unsupported_worker_guess(candidate):
        return "unsupported TypeScript bridge guess excluded until worker-backed semantics are available"
    return None


def _report_filter(candidate: Candidate) -> str | None:
    return None


def _filter_candidates(candidates: Iterable[Candidate], mode: PlanMode) -> tuple[list[Candidate], list[ExcludedCandidate]]:
    filter_fn = {
        "safe": _safe_filter,
        "balanced": _balanced_filter,
        "report": _report_filter,
    }[mode]
    selected: list[Candidate] = []
    excluded: list[ExcludedCandidate] = []
    for candidate in sorted(candidates, key=_ranking_key):
        reason = filter_fn(candidate)
        if reason is None:
            selected.append(candidate)
            continue
        if mode == "safe":
            continue
        excluded.append(ExcludedCandidate(candidate=candidate, reason=reason))
    return selected, excluded


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


def _conflict_edge(left: Candidate, right: Candidate, reason: str) -> PlanEdge:
    first_id, second_id = sorted((left.id, right.id))
    return PlanEdge(fromId=first_id, toId=second_id, kind="conflict", reason=reason)



def _conflict_edges(candidates: list[Candidate]) -> list[PlanEdge]:
    edges: list[PlanEdge] = []
    seen: set[tuple[str, str, str]] = set()
    by_id = {candidate.id: candidate for candidate in candidates}

    def add_edge(left: Candidate, right: Candidate, reason: str) -> None:
        edge = _conflict_edge(left, right, reason)
        key = (edge.from_id, edge.to_id, edge.reason)
        if key not in seen:
            seen.add(key)
            edges.append(edge)

    for index, left in enumerate(candidates):
        for right in candidates[index + 1 :]:
            if _regions_overlap(left, right):
                add_edge(left, right, "overlapping anchor regions in the same file")
                continue
            if _shared_symbol_scope(left, right):
                add_edge(left, right, "same symbol in the same language scope")
                continue
            if _same_file_non_local(left, right):
                add_edge(left, right, "same file touched with at least one non-local candidate")
        for conflict_id in left.conflicts:
            if conflict_id in by_id and conflict_id != left.id:
                add_edge(left, by_id[conflict_id], "explicit conflict declared by candidate")
    return edges


def _dependency_edges(candidates: list[Candidate]) -> list[PlanEdge]:
    by_id = {candidate.id: candidate for candidate in candidates}
    edges: list[PlanEdge] = []
    for candidate in candidates:
        for dependency_id in candidate.dependencies:
            if dependency_id not in by_id:
                continue
            edges.append(
                PlanEdge(
                    fromId=candidate.id,
                    toId=dependency_id,
                    kind="dependency",
                    reason="explicit dependency declared by candidate",
                )
            )
    return edges


def _required_checks(candidates: list[Candidate]) -> list[str]:
    ordered: dict[str, None] = {}
    for candidate in candidates:
        for check in candidate.required_checks:
            ordered.setdefault(check, None)
    return list(ordered)


def build_plan(*, mode: PlanMode, repo: RepoSnapshot, adapter_names: list[str], candidates: list[Candidate]) -> PlanResult:
    selected, excluded = _filter_candidates(candidates, mode)
    edges = _conflict_edges(selected)
    edges.extend(_dependency_edges(selected))
    ordered_edges = sorted(edges, key=lambda edge: (edge.kind, edge.from_id, edge.to_id, edge.reason))
    return PlanResult(
        mode=mode,
        repo=repo,
        adapterNames=adapter_names,
        selectedCandidates=selected,
        excludedCandidates=excluded,
        edges=ordered_edges,
        requiredChecks=_required_checks(selected),
        candidateCount=len(candidates),
        selectedCount=len(selected),
        excludedCount=len(excluded),
    )
