from __future__ import annotations

from pathlib import Path
from typing import cast

from refactorq.agents.codex import CodexGuardedApplier
from refactorq.core.candidate import Candidate
from refactorq.core.candidate.models import VerificationCheck

from refactorq.core.planning import PlanResult
from refactorq.core.verification import VerificationResult
from refactorq.core.verification.service import verify_repo

from .models import (
    ApplyResult,
    ExecutionCandidateNote,
)
from .auto_patch import apply_auto_candidate
from .guarded import apply_guarded_candidate
from .snapshot import changed_paths, snapshot_repo
from .support import candidate_support_reason


class _ApplyInternalResult(ApplyResult):
    original_snapshot: dict[str, bytes] = {}


def _candidate_line_number(candidate: Candidate) -> int:
    if not candidate.anchor_regions:
        return 0
    return candidate.anchor_regions[0].start_line


def _candidate_sort_key(candidate: Candidate) -> tuple[int, str, int, str]:
    apply_priority = 0 if candidate.apply_mode_hint == "auto" else 1
    file_name = candidate.files[0] if candidate.files else ""
    return (apply_priority, file_name, -_candidate_line_number(candidate), candidate.id)


def _verify_for_execution(root: Path, plan: PlanResult, candidates: list[Candidate]) -> VerificationResult:
    required_checks: list[VerificationCheck] = []
    for candidate in candidates:
        for check in candidate.required_checks:
            if check not in required_checks:
                required_checks.append(check)
    if not required_checks:
        required_checks = cast(list[VerificationCheck], list(plan.required_checks))
    return verify_repo(root, required_checks=required_checks, candidates=candidates)


def _solver_selected_candidates(plan: PlanResult) -> list[Candidate]:
    if plan.solver_proposal is None:
        return []
    proposed_by_id = {candidate.id: candidate for candidate in plan.solver_proposal.candidates}
    return [
        proposed_by_id[candidate_id]
        for candidate_id in plan.solver_proposal.selected_candidate_ids
        if candidate_id in proposed_by_id
    ]


def _optimizer_rejection_notes(plan: PlanResult) -> list[ExecutionCandidateNote]:
    rejection_reason = (
        "; ".join(plan.proposal_revalidation.rejection_reasons)
        or "planner revalidation rejected the optimizer proposal"
    )
    return [
        ExecutionCandidateNote(candidate=candidate, reason=rejection_reason)
        for candidate in _solver_selected_candidates(plan)
    ]


def _apply_plan_internal(root: Path, plan: PlanResult) -> _ApplyInternalResult:
    applied: list[Candidate] = []
    skipped: list[ExecutionCandidateNote] = []
    file_lines: dict[str, list[str]] = {}
    original_snapshot = snapshot_repo(root)
    guarded_applier = CodexGuardedApplier()
    if plan.selection_source == "optimizer_rejected_no_batch" and not plan.selected_candidates:
        return _ApplyInternalResult(
            mode=plan.mode,
            repo=plan.repo,
            plan=plan,
            status="rejected_no_batch",
            appliedCandidates=[],
            skippedCandidates=_optimizer_rejection_notes(plan),
            changedFiles=[],
            original_snapshot=original_snapshot,
        )
    changed_files_since_scan: set[str] = set()
    for candidate in sorted(plan.selected_candidates, key=_candidate_sort_key):
        reason = candidate_support_reason(root, candidate, guarded_applier)
        if candidate.apply_mode_hint == "auto":
            if reason is not None:
                skipped.append(ExecutionCandidateNote(candidate=candidate, reason=reason))
                continue
            rel_path = candidate.files[0]
            if rel_path not in file_lines:
                current_text = (root / rel_path).read_text(encoding="utf-8")
                file_lines[rel_path] = current_text.splitlines(keepends=True)
            updated_lines, changed = apply_auto_candidate(file_lines[rel_path], candidate)
            if not changed:
                skipped.append(
                    ExecutionCandidateNote(candidate=candidate, reason="candidate produced no deterministic file change")
                )
                continue
            file_lines[rel_path] = updated_lines
            (root / rel_path).write_text("".join(updated_lines), encoding="utf-8")
            applied.append(candidate)
            changed_files_since_scan.add(rel_path)
            continue

        if candidate.apply_mode_hint == "guarded":
            if reason is not None:
                skipped.append(ExecutionCandidateNote(candidate=candidate, reason=reason))
                continue
            if any(rel_path in changed_files_since_scan for rel_path in candidate.files):
                skipped.append(
                    ExecutionCandidateNote(
                        candidate=candidate,
                        reason="guarded candidate anchors require re-scan after earlier same-file edits",
                    )
                )
                continue
            applied_guarded, reason = apply_guarded_candidate(root, candidate, guarded_applier)
            if not applied_guarded:
                skipped.append(
                    ExecutionCandidateNote(candidate=candidate, reason=reason or "guarded Codex flow skipped candidate")
                )
                continue
            applied.append(candidate)
            for rel_path in candidate.files:
                changed_files_since_scan.add(rel_path)
            continue

        skipped.append(ExecutionCandidateNote(candidate=candidate, reason=reason or "report-only candidate is not applied"))
    changed_files = changed_paths(original_snapshot, snapshot_repo(root))
    return _ApplyInternalResult(
        mode=plan.mode,
        repo=plan.repo,
        plan=plan,
        status="applied" if changed_files else "no_changes",
        appliedCandidates=applied,
        skippedCandidates=skipped,
        changedFiles=changed_files,
        original_snapshot=original_snapshot,
    )


def apply_plan(root: Path, plan: PlanResult) -> ApplyResult:
    result = _apply_plan_internal(root, plan)
    return ApplyResult.model_validate(result.model_dump(by_alias=True))


def _public_apply_result(apply_result: _ApplyInternalResult) -> ApplyResult:
    return ApplyResult.model_validate(apply_result.model_dump(by_alias=True))

