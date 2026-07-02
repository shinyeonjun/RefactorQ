from __future__ import annotations

from pathlib import Path

from refactorq.agents.codex import CodexGuardedApplier
from refactorq.core.candidate import Candidate
from refactorq.core.candidate.models import VerificationCheck
from refactorq.core.git_execution import inspect_git_workspace
from refactorq.core.planning import PlanResult
from refactorq.core.verification.service import build_verification_report

from .models import (
    BoundaryExecutionSummary,
    ExecutionSupportSummary,
    ReportResult,
    VerificationPlanSummary,
)
from .support import candidate_support_reason


def _solver_selected_candidates(plan: PlanResult) -> list[Candidate]:
    if plan.solver_proposal is None:
        return []
    proposed_by_id = {candidate.id: candidate for candidate in plan.solver_proposal.candidates}
    return [
        proposed_by_id[candidate_id]
        for candidate_id in plan.solver_proposal.selected_candidate_ids
        if candidate_id in proposed_by_id
    ]


def _report_scope_candidates(plan: PlanResult) -> list[Candidate]:
    if plan.selection_source != "optimizer_rejected_no_batch" or plan.solver_proposal is None:
        return list(plan.selected_candidates)
    return _solver_selected_candidates(plan)


def _required_checks(candidates: list[Candidate]) -> list[VerificationCheck]:
    checks: list[VerificationCheck] = []
    for candidate in candidates:
        for check in candidate.required_checks:
            if check not in checks:
                checks.append(check)
    return checks


def report_plan(root: Path, plan: PlanResult) -> ReportResult:
    supported = 0
    supported_auto = 0
    supported_guarded = 0
    unsupported = 0
    blocked_boundary = 0
    ready_boundary = 0
    contract_ready = 0
    contract_blocked = 0
    cross_language = 0
    boundary_sensitive = 0
    highest_impact = "none"
    blocked_reasons: dict[str, None] = {}
    kinds: set[str] = set()
    guarded_applier = CodexGuardedApplier()
    git_state = inspect_git_workspace(root)
    impact_priority = {"none": 0, "low": 1, "medium": 2, "high": 3}
    report_candidates = _report_scope_candidates(plan)
    verification_plan = VerificationPlanSummary.model_validate(
        build_verification_report(
            root,
            required_checks=_required_checks(report_candidates),
            candidates=report_candidates,
        )
    )
    boundary_candidates_by_id = {
        candidate_payload.candidate_id: candidate_payload for candidate_payload in verification_plan.boundary_candidates
    }
    linked_contract_artifacts = sorted(
        {
            artifact
            for candidate_payload in verification_plan.boundary_candidates
            for artifact in candidate_payload.contract_artifacts
        }
    )
    for candidate in report_candidates:
        boundary_candidate = boundary_candidates_by_id.get(candidate.id)
        boundary_sensitive_candidate = (
            candidate.boundary_impact.cross_language or candidate.boundary_impact.impact_level != "none"
        )
        if candidate.boundary_impact.cross_language:
            cross_language += 1
        if boundary_sensitive_candidate:
            boundary_sensitive += 1
        if impact_priority[candidate.boundary_impact.impact_level] > impact_priority[highest_impact]:
            highest_impact = candidate.boundary_impact.impact_level

        reason = candidate_support_reason(root, candidate, guarded_applier)
        if reason is None and boundary_candidate is not None and boundary_candidate.missing_predicates:
            reason = (
                boundary_candidate.blocked_reasons[0]
                if boundary_candidate.blocked_reasons
                else boundary_candidate.missing_predicates[0]
            )
        if reason is None:
            supported += 1
            kinds.add(candidate.kind)
            if candidate.apply_mode_hint == "auto":
                supported_auto += 1
            elif candidate.apply_mode_hint == "guarded":
                supported_guarded += 1
            if boundary_sensitive_candidate:
                ready_boundary += 1
            if candidate.boundary_impact.cross_language and boundary_candidate is not None and boundary_candidate.proof_refs:
                contract_ready += 1
            continue

        unsupported += 1
        if boundary_sensitive_candidate:
            blocked_boundary += 1
            blocked_reasons.setdefault(reason, None)
            if boundary_candidate is not None:
                for blocked_reason in boundary_candidate.blocked_reasons:
                    blocked_reasons.setdefault(blocked_reason, None)
        if candidate.boundary_impact.cross_language:
            contract_blocked += 1
    if plan.selection_source == "optimizer_rejected_no_batch":
        for rejection_reason in plan.proposal_revalidation.rejection_reasons:
            blocked_reasons.setdefault(rejection_reason, None)

    return ReportResult(
        mode=plan.mode,
        repo=plan.repo,
        plan=plan,
        executionSupport=ExecutionSupportSummary(
            supportedCandidates=supported,
            supportedAutoCandidates=supported_auto,
            supportedGuardedCandidates=supported_guarded,
            unsupportedCandidates=unsupported,
            appliedCandidateKinds=sorted(kinds),
            gitBranchingSupported=git_state.available and git_state.clean,
            gitReason=git_state.reason,
        ),
        boundaryExecution=BoundaryExecutionSummary(
            crossLanguageCandidates=cross_language,
            boundarySensitiveCandidates=boundary_sensitive,
            blockedBoundaryCandidates=blocked_boundary,
            readyBoundaryCandidates=ready_boundary,
            contractReadyCandidates=contract_ready,
            contractBlockedCandidates=contract_blocked,
            contractArtifacts=linked_contract_artifacts,
            blockedReasons=list(blocked_reasons),
            highestImpact=highest_impact,
            proofStatus=verification_plan.proof_status,
            missingPredicates=verification_plan.missing_predicates,
            proofRefs=verification_plan.proof_refs,
        ),
        verificationPlan=verification_plan,
    )
