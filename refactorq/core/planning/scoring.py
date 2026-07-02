from __future__ import annotations

from refactorq.core.candidate import Candidate


APPLY_MODE_PRIORITY = {"auto": 0, "guarded": 1, "report_only": 2}
IMPACT_PRIORITY = {"none": 0, "low": 1, "medium": 2, "high": 3}


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


def candidate_diff_lines(candidate: Candidate) -> int:
    diff = candidate.estimated_diff
    return diff.lines_modified + diff.lines_added + diff.lines_deleted


def candidate_score(candidate: Candidate) -> float:
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
        - 0.001 * candidate_diff_lines(candidate)
        - 0.05 * IMPACT_PRIORITY[candidate.boundary_impact.impact_level]
        - 0.04 * verification_burden
    )


def is_high_risk(candidate: Candidate) -> bool:
    risk = candidate.estimated_risk
    return (
        risk.semantic_risk >= 0.4
        or risk.api_risk >= 0.25
        or risk.runtime_risk >= 0.3
        or candidate.boundary_impact.impact_level in {"medium", "high"}
    )


def ranking_key(candidate: Candidate) -> tuple[object, ...]:
    risk = candidate.estimated_risk
    diff = candidate.estimated_diff
    return (
        -candidate_score(candidate),
        APPLY_MODE_PRIORITY[candidate.apply_mode_hint],
        IMPACT_PRIORITY[candidate.boundary_impact.impact_level],
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
