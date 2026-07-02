from __future__ import annotations

import difflib
import json
import subprocess
from pathlib import Path

from pydantic import ValidationError

from refactorq.agents.codex import CodexGuardedApplier, GuardedExecutionContractError
from refactorq.core.candidate import Candidate
from refactorq.core.verification import VerificationResult

from .models import RepairResult
from .snapshot import changed_paths, restore_snapshot, snapshot_repo


class RepairAttempt:
    def __init__(self, result: RepairResult, repaired: bool) -> None:
        self.result = result
        self.repaired = repaired


def _failed_repair(touched_files: list[str], reason: str) -> RepairAttempt:
    return RepairAttempt(
        RepairResult(status="failed", attempted=True, touchedFiles=touched_files, reason=reason),
        repaired=False,
    )


def _skipped_repair(*, attempted: bool, reason: str, touched_files: list[str] | None = None) -> RepairAttempt:
    return RepairAttempt(
        RepairResult(status="skipped", attempted=attempted, touchedFiles=touched_files or [], reason=reason),
        repaired=False,
    )


def _repaired(touched_files: list[str]) -> RepairAttempt:
    return RepairAttempt(RepairResult(status="repaired", attempted=True, touchedFiles=touched_files), repaired=True)


def _line_count(content: bytes | None) -> int:
    if not content:
        return 0
    return len(content.splitlines())


def _changed_line_count(before: bytes | None, after: bytes | None) -> int:
    matcher = difflib.SequenceMatcher(a=(before or b"").splitlines(), b=(after or b"").splitlines())
    changed_lines = 0
    for tag, before_start, before_end, after_start, after_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        changed_lines += max(before_end - before_start, after_end - after_start)
    return changed_lines


def _guarded_expected_line_budget(candidate: Candidate) -> int:
    anchor_lines = sum(max(region.end_line - region.start_line + 1, 0) for region in candidate.anchor_regions)
    estimated_lines = (
        candidate.estimated_diff.lines_added
        + candidate.estimated_diff.lines_deleted
        + candidate.estimated_diff.lines_modified
    )
    return max(anchor_lines, estimated_lines, 1)


def _same_file_diff_reason(
    before: dict[str, bytes],
    after: dict[str, bytes],
    candidates: list[Candidate],
    operation: str,
) -> str | None:
    candidates_by_file: dict[str, list[Candidate]] = {}
    for candidate in candidates:
        for rel_path in candidate.files:
            candidates_by_file.setdefault(rel_path, []).append(candidate)
    for rel_path, file_candidates in candidates_by_file.items():
        if before.get(rel_path) == after.get(rel_path):
            continue
        total_lines = max(_line_count(before.get(rel_path)), _line_count(after.get(rel_path)))
        if total_lines < 20:
            continue
        changed_lines = _changed_line_count(before.get(rel_path), after.get(rel_path))
        expected_lines = sum(_guarded_expected_line_budget(candidate) for candidate in file_candidates)
        allowed_changed_lines = max(24, expected_lines * 3)
        if changed_lines > allowed_changed_lines and changed_lines * 5 >= total_lines * 4:
            return f"{operation} exceeded the same-file diff safety budget before verification ({rel_path})"
    return None


def _failure_reason(error: Exception) -> str:
    if isinstance(error, GuardedExecutionContractError):
        return str(error)
    if isinstance(error, subprocess.TimeoutExpired):
        return "Codex guarded execution timed out"
    if isinstance(error, subprocess.CalledProcessError):
        stderr = error.stderr.decode("utf-8", errors="replace") if isinstance(error.stderr, bytes) else error.stderr
        stdout = error.stdout.decode("utf-8", errors="replace") if isinstance(error.stdout, bytes) else error.stdout
        detail = (stderr or stdout or "")[:200].strip()
        return f"Codex guarded execution failed{': ' + detail if detail else ''}"
    if isinstance(error, FileNotFoundError):
        return "codex cli is not available"
    if isinstance(error, json.JSONDecodeError):
        return "Codex guarded execution returned malformed JSON"
    if isinstance(error, ValidationError):
        return "Codex guarded execution returned an invalid structured response"
    return f"Codex guarded execution failed: {error}"


def apply_guarded_candidate(root: Path, candidate: Candidate, guarded_applier: CodexGuardedApplier) -> tuple[bool, str | None]:
    support_reason = guarded_applier.support_reason(root, candidate)
    if support_reason is not None:
        return False, support_reason

    before = snapshot_repo(root)
    try:
        result = guarded_applier.apply(root, candidate)
    except Exception as exc:  # pragma: no cover
        restore_snapshot(root, before)
        return False, _failure_reason(exc)

    after = snapshot_repo(root)
    changed = changed_paths(before, after)
    allowed_files = set(candidate.files)
    allowed_candidate_ids = {candidate.id}
    touched_files = set(result.touched_files)
    declared_candidate_ids = set(result.candidate_ids)

    if result.status == "unsupported":
        restore_snapshot(root, before)
        reason = result.summary[0] if result.summary else "guarded Codex flow reported unsupported"
        return False, reason
    if not declared_candidate_ids:
        restore_snapshot(root, before)
        return False, "guarded Codex response omitted candidateIds for the selected candidate scope"
    if any(candidate_id not in allowed_candidate_ids for candidate_id in declared_candidate_ids):
        restore_snapshot(root, before)
        return False, "guarded Codex response declared candidateIds outside the selected candidate scope"

    if any(path not in allowed_files for path in changed):
        restore_snapshot(root, before)
        return False, "guarded Codex flow touched files outside the allowed candidate scope"
    if any(path not in allowed_files for path in touched_files):
        restore_snapshot(root, before)
        return False, "guarded Codex response declared files outside the allowed candidate scope"
    if result.status == "no_change":
        if changed:
            restore_snapshot(root, before)
            return False, "guarded Codex response reported no_change despite modifying the repo"
        if touched_files:
            return False, "guarded Codex response touchedFiles did not match the actual changed files"
        reason = result.summary[0] if result.summary else "guarded Codex flow reported no changes"
        return False, reason
    if touched_files != set(changed):
        restore_snapshot(root, before)
        return False, "guarded Codex response touchedFiles did not match the actual changed files"
    if not changed:
        return False, "guarded Codex flow produced no file changes"
    diff_reason = _same_file_diff_reason(before, after, [candidate], "guarded Codex flow")
    if diff_reason is not None:
        restore_snapshot(root, before)
        return False, diff_reason
    return True, None


def repair_guarded_changes(
    root: Path,
    guarded_candidates: list[Candidate],
    verification: VerificationResult,
    guarded_applier: CodexGuardedApplier,
) -> RepairAttempt:
    if not guarded_candidates:
        return RepairAttempt(RepairResult(status="not_needed", attempted=False, touchedFiles=[]), repaired=False)
    if not guarded_applier.is_available():
        return _skipped_repair(attempted=False, reason="codex cli is not available")

    before = snapshot_repo(root)
    allowed_files = {file for candidate in guarded_candidates for file in candidate.files}
    allowed_candidate_ids = {candidate.id for candidate in guarded_candidates}
    try:
        result = guarded_applier.repair(root, guarded_candidates, verification)
    except Exception as exc:  # pragma: no cover
        restore_snapshot(root, before)
        return _failed_repair([], _failure_reason(exc))

    after = snapshot_repo(root)
    changed = changed_paths(before, after)
    touched_files = set(result.touched_files)
    declared_candidate_ids = set(result.candidate_ids)

    if result.status == "unsupported":
        restore_snapshot(root, before)
        return _skipped_repair(
            attempted=True,
            reason=result.summary[0] if result.summary else "guarded repair is unsupported",
        )
    if not declared_candidate_ids:
        restore_snapshot(root, before)
        return _failed_repair(
            sorted(touched_files),
            "guarded Codex repair omitted candidateIds for the selected candidate scope",
        )
    if declared_candidate_ids != allowed_candidate_ids:
        restore_snapshot(root, before)
        return _failed_repair(
            sorted(touched_files),
            "guarded Codex repair candidateIds did not match the selected candidate scope",
        )
    if any(path not in allowed_files for path in changed):
        restore_snapshot(root, before)
        return _failed_repair(
            changed,
            "guarded Codex repair touched files outside the allowed candidate scope",
        )
    if any(path not in allowed_files for path in touched_files):
        restore_snapshot(root, before)
        return _failed_repair(
            sorted(touched_files),
            "guarded Codex repair declared files outside the allowed candidate scope",
        )
    if result.status == "no_change":
        if changed:
            restore_snapshot(root, before)
            return _failed_repair(
                changed,
                "guarded Codex repair reported no_change despite modifying the repo",
            )
        if touched_files:
            return _failed_repair(
                sorted(touched_files),
                "guarded Codex repair touchedFiles did not match the actual changed files",
            )
        return _skipped_repair(
            attempted=True,
            reason=result.summary[0] if result.summary else "guarded Codex repair made no changes",
        )
    if touched_files != set(changed):
        restore_snapshot(root, before)
        return _failed_repair(
            sorted(touched_files),
            "guarded Codex repair touchedFiles did not match the actual changed files",
        )
    if not changed:
        return _skipped_repair(attempted=True, reason="guarded Codex repair made no changes")
    diff_reason = _same_file_diff_reason(before, after, guarded_candidates, "guarded Codex repair")
    if diff_reason is not None:
        restore_snapshot(root, before)
        return _failed_repair(changed, diff_reason)
    return _repaired(changed)
