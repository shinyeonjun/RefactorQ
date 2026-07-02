from __future__ import annotations

from pathlib import Path

from refactorq.agents.codex import SUPPORTED_GUARDED_KINDS, CodexGuardedApplier
from refactorq.core.candidate import Candidate

from .auto_patch import auto_support_reason


_CROSS_LANGUAGE_GUARDED_KINDS = set(SUPPORTED_GUARDED_KINDS)


def _boundary_support_reason(candidate: Candidate) -> str | None:
    if not candidate.boundary_impact.cross_language:
        return None
    if not candidate.boundary_impact.contract_artifacts:
        return "cross-language candidate requires explicit boundary contract artifacts"
    if candidate.boundary_impact.impact_level not in {"none", "low"}:
        return "cross-language candidate requires low boundary impact for deterministic execution"
    if (
        candidate.apply_mode_hint == "guarded"
        and (
            candidate.kind not in _CROSS_LANGUAGE_GUARDED_KINDS
            or len(candidate.files) != 1
            or candidate.scope not in {"local", "module"}
        )
    ):
        return "guarded cross-language candidate is not yet supported for boundary-aware execution"
    return None


def candidate_support_reason(root: Path, candidate: Candidate, guarded_applier: CodexGuardedApplier) -> str | None:
    boundary_reason = _boundary_support_reason(candidate)
    if boundary_reason is not None:
        return boundary_reason
    if candidate.apply_mode_hint == "auto":
        return auto_support_reason(root, candidate)
    if candidate.apply_mode_hint == "guarded":
        return guarded_applier.support_reason(root, candidate)
    return "report-only candidate is not applied"
