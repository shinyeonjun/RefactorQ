from __future__ import annotations

import subprocess
from pathlib import Path

from refactorq.agents.codex import CodexGuardedApplier
from refactorq.core.git_execution import (
    GitExecutionContext,
    abort_git_execution,
    begin_git_execution,
    finalize_git_execution,
    inspect_git_workspace,
)
from refactorq.core.planning import PlanResult
from refactorq.core.verification import VerificationReadiness, VerificationResult

from .guarded import repair_guarded_changes
from .models import GitExecutionResult, RepairResult, RunResult, RunStatus
from .service import _ApplyInternalResult, _apply_plan_internal, _public_apply_result, _verify_for_execution
from .snapshot import restore_snapshot


def _run_result(
    *,
    plan: PlanResult,
    apply_result: _ApplyInternalResult,
    verification: VerificationResult,
    status: RunStatus,
    rollback_applied: bool,
    repair_result: RepairResult,
    git_result: GitExecutionResult,
) -> RunResult:
    return RunResult(
        mode=plan.mode,
        repo=plan.repo,
        plan=plan,
        apply=_public_apply_result(apply_result),
        verification=verification,
        status=status,
        executedSelectionSource=plan.selection_source,
        rollbackApplied=rollback_applied,
        repair=repair_result,
        git=git_result,
    )


def _initial_git_result(root: Path) -> GitExecutionResult:
    state = inspect_git_workspace(root)
    return GitExecutionResult(
        enabled=state.available and state.clean,
        available=state.available,
        clean=state.clean,
        baseBranch=state.base_branch,
        reason=state.reason,
    )


def _abort_branch_if_needed(root: Path, context: GitExecutionContext | None) -> None:
    if context is None:
        return
    abort_git_execution(root, context)


def run_plan(root: Path, plan: PlanResult) -> RunResult:
    git_result = _initial_git_result(root)
    git_context = begin_git_execution(root, plan.mode) if git_result.enabled else None
    if git_context is not None:
        git_result.execution_branch = git_context.execution_branch

    apply_result = _apply_plan_internal(root, plan)
    verification: VerificationResult
    rollback_applied = False
    repair_result = RepairResult(status="not_needed", attempted=False, touchedFiles=[])

    if apply_result.status in {"no_changes", "rejected_no_batch"}:
        _abort_branch_if_needed(root, git_context)
        verification = VerificationResult(
            status="skipped",
            checks=[],
            readiness=VerificationReadiness(
                ready=True,
                proofStatus="not_applicable",
                missingPredicates=[],
                proofRefs=[],
            ),
            proofRecords=[],
        )
        run_status: RunStatus = "rejected_no_batch" if apply_result.status == "rejected_no_batch" else "no_changes"
        return _run_result(
            plan=plan,
            apply_result=apply_result,
            verification=verification,
            status=run_status,
            rollback_applied=False,
            repair_result=repair_result,
            git_result=git_result,
        )

    verification = _verify_for_execution(root, plan, apply_result.applied_candidates)
    guarded_candidates = [candidate for candidate in apply_result.applied_candidates if candidate.apply_mode_hint == "guarded"]
    if verification.status == "failed":
        repair_attempt = repair_guarded_changes(root, guarded_candidates, verification, CodexGuardedApplier())
        repair_result = repair_attempt.result
        if repair_attempt.repaired:
            verification = _verify_for_execution(root, plan, apply_result.applied_candidates)

    if verification.status == "failed":
        rollback_applied = restore_snapshot(root, apply_result.original_snapshot)
        _abort_branch_if_needed(root, git_context)
        return _run_result(
            plan=plan,
            apply_result=apply_result,
            verification=verification,
            status="rolled_back",
            rollback_applied=rollback_applied,
            repair_result=repair_result,
            git_result=git_result,
        )

    if git_context is not None:
        try:
            git_result.commit_sha = finalize_git_execution(root, git_context, apply_result.changed_files, plan.mode)
        except subprocess.CalledProcessError:
            rollback_applied = restore_snapshot(root, apply_result.original_snapshot)
            _abort_branch_if_needed(root, git_context)
            return _run_result(
                plan=plan,
                apply_result=apply_result,
                verification=verification,
                status="rolled_back",
                rollback_applied=rollback_applied,
                repair_result=repair_result,
                git_result=GitExecutionResult(
                    enabled=git_result.enabled,
                    available=git_result.available,
                    clean=git_result.clean,
                    baseBranch=git_result.base_branch,
                    executionBranch=git_result.execution_branch,
                    reason="git commit failed after successful verification",
                ),
            )

    return _run_result(
        plan=plan,
        apply_result=apply_result,
        verification=verification,
        status="passed",
        rollback_applied=False,
        repair_result=repair_result,
        git_result=git_result,
    )
