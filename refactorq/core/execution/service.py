from __future__ import annotations

import difflib
import json
import re
import subprocess
from pathlib import Path

from pydantic import ValidationError

from refactorq.agents.codex import SUPPORTED_GUARDED_KINDS, CodexGuardedApplier, GuardedExecutionContractError
from refactorq.core.candidate import Candidate
from refactorq.core.candidate.models import VerificationCheck

from refactorq.core.filesystem import walk_repo_files
from refactorq.core.git_execution import (
    GitExecutionContext,
    begin_git_execution,
    finalize_git_execution,
    inspect_git_workspace,
    abort_git_execution,
)
from refactorq.core.planning import PlanResult
from refactorq.core.verification import VerificationReadiness, VerificationResult
from refactorq.core.verification.service import build_verification_report, verify_repo

from .models import (
    ApplyResult,
    BoundaryExecutionSummary,
    ExecutionCandidateNote,
    ExecutionSupportSummary,
    GitExecutionResult,
    RepairResult,
    ReportResult,
    RunResult,
    VerificationPlanSummary,
)

_IMPORT_PREFIXES = ("import ", "from ")
_TS_IMPORT_PATTERN = re.compile(
    r'^(?P<indent>\s*)import\s+(?P<clause>.+?)\s+from\s+(?P<source>["\'][^"\']+["\'];?\s*)$'
)

_CROSS_LANGUAGE_GUARDED_KINDS = set(SUPPORTED_GUARDED_KINDS)


class _ApplyInternalResult(ApplyResult):
    original_snapshot: dict[str, bytes] = {}


class _RepairAttempt:
    def __init__(self, result: RepairResult, repaired: bool) -> None:
        self.result = result
        self.repaired = repaired


def _candidate_line_number(candidate: Candidate) -> int:
    if not candidate.anchor_regions:
        return 0
    return candidate.anchor_regions[0].start_line


def _candidate_sort_key(candidate: Candidate) -> tuple[int, str, int, str]:
    apply_priority = 0 if candidate.apply_mode_hint == "auto" else 1
    file_name = candidate.files[0] if candidate.files else ""
    return (apply_priority, file_name, -_candidate_line_number(candidate), candidate.id)


def _looks_like_single_line_import(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and stripped.startswith(_IMPORT_PREFIXES) and "\\" not in stripped


def _python_bound_name(specifier: str) -> str:
    specifier = specifier.strip()
    if " as " in specifier:
        return specifier.rsplit(" as ", 1)[1].strip()
    return specifier.split(".", 1)[0].strip()


def _rewrite_python_import(line: str, symbol: str) -> str | None:
    if "#" in line or "(" in line or ")" in line:
        return None
    stripped = line.strip()
    indent = line[: len(line) - len(line.lstrip())]
    newline = "\n" if line.endswith("\n") else ""
    if stripped.startswith("import "):
        specifiers = [part.strip() for part in stripped[len("import ") :].split(",")]
        kept = [specifier for specifier in specifiers if _python_bound_name(specifier) != symbol]
        if len(kept) == len(specifiers):
            return None
        if not kept:
            return ""
        return f"{indent}import {', '.join(kept)}{newline}"
    if stripped.startswith("from ") and " import " in stripped:
        prefix, rest = stripped.split(" import ", 1)
        specifiers = [part.strip() for part in rest.split(",")]
        kept = [specifier for specifier in specifiers if _python_bound_name(specifier) != symbol]
        if len(kept) == len(specifiers):
            return None
        if not kept:
            return ""
        return f"{indent}{prefix} import {', '.join(kept)}{newline}"
    return None


def _ts_bound_name(specifier: str) -> str:
    specifier = specifier.strip()
    if " as " in specifier:
        return specifier.rsplit(" as ", 1)[1].strip()
    return specifier.strip()


def _rewrite_typescript_import(line: str, symbol: str) -> str | None:
    if "//" in line or "/*" in line or "*/" in line:
        return None
    match = _TS_IMPORT_PATTERN.match(line.rstrip("\n"))
    if match is None:
        return None
    indent = match.group("indent")
    clause = match.group("clause").strip()
    source = match.group("source")
    newline = "\n" if line.endswith("\n") else ""

    type_prefix = ""
    if clause.startswith("type "):
        type_prefix = "type "
        clause = clause[len("type ") :].strip()

    if clause.startswith("* as "):
        return "" if clause[len("* as ") :].strip() == symbol else None

    default_part: str | None = None
    named_part: str | None = None
    if "{" in clause and "}" in clause:
        default_prefix, named_block = clause.split("{", 1)
        named_body, suffix = named_block.split("}", 1)
        if suffix.strip():
            return None
        default_prefix = default_prefix.rstrip(", ").strip()
        default_part = default_prefix or None
        named_part = named_body.strip()
    else:
        default_part = clause

    changed = False
    if default_part and default_part == symbol:
        default_part = None
        changed = True

    named_specifiers: list[str] = []
    if named_part is not None:
        raw_specifiers = [part.strip() for part in named_part.split(",") if part.strip()]
        named_specifiers = [specifier for specifier in raw_specifiers if _ts_bound_name(specifier) != symbol]
        if len(named_specifiers) != len(raw_specifiers):
            changed = True

    if not changed:
        return None
    if default_part is None and named_part is None:
        return ""
    import_prefix = f"{indent}import {type_prefix}"
    if default_part is not None and named_part is None:
        return f"{import_prefix}{default_part} from {source}{newline}"
    if default_part is None and not named_specifiers:
        return ""
    if default_part is None:
        return f"{import_prefix}{{ {', '.join(named_specifiers)} }} from {source}{newline}"
    if not named_specifiers:
        return f"{import_prefix}{default_part} from {source}{newline}"
    return f"{import_prefix}{default_part}, {{ {', '.join(named_specifiers)} }} from {source}{newline}"


def _rewrite_unused_import_line(candidate: Candidate, line: str) -> str | None:
    if candidate.language == "python":
        return _rewrite_python_import(line, candidate.symbols[0])
    if candidate.language in {"typescript", "javascript"}:
        return _rewrite_typescript_import(line, candidate.symbols[0])
    return None


def _auto_support_reason(root: Path, candidate: Candidate) -> str | None:
    if candidate.apply_mode_hint != "auto":
        return "candidate requires guarded handling"
    if candidate.kind not in {"unused_import", "dead_code", "unused_symbol"}:
        return "deterministic patcher currently supports unused_import, dead_code, and unused_symbol only"
    if len(candidate.files) != 1 or len(candidate.anchor_regions) != 1 or len(candidate.symbols) != 1:
        return "candidate does not target a single file, region, and symbol"
    region = candidate.anchor_regions[0]
    target = root / candidate.files[0]
    if not target.exists():
        return "candidate target file is missing"
    lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
    if region.start_line < 1 or region.end_line > len(lines):
        return "candidate line falls outside the current file"
    if candidate.kind == "unused_import":
        if region.start_line != region.end_line:
            return "candidate spans multiple lines"
        line = lines[region.start_line - 1]
        if not _looks_like_single_line_import(line):
            return "candidate import statement is not a supported single-line import"
        if _rewrite_unused_import_line(candidate, line) is None:
            return "candidate import statement cannot be rewritten deterministically"
        return None
    if candidate.language == "python" and candidate.kind in {"dead_code", "unused_symbol"}:
        return None
    if candidate.language in {"typescript", "javascript"} and candidate.kind == "unused_symbol":
        return None
    return "candidate kind is not supported for this language"


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


def _candidate_support_reason(root: Path, candidate: Candidate, guarded_applier: CodexGuardedApplier) -> str | None:
    boundary_reason = _boundary_support_reason(candidate)
    if boundary_reason is not None:
        return boundary_reason
    if candidate.apply_mode_hint == "auto":
        return _auto_support_reason(root, candidate)
    if candidate.apply_mode_hint == "guarded":
        return guarded_applier.support_reason(root, candidate)
    return "report-only candidate is not applied"


def _verify_for_execution(root: Path, plan: PlanResult, candidates: list[Candidate]) -> VerificationResult:
    boundary_candidates = [
        candidate
        for candidate in candidates
        if candidate.boundary_impact.cross_language or candidate.boundary_impact.impact_level != "none"
    ]
    if not boundary_candidates:
        return verify_repo(root)

    required_checks: list[VerificationCheck] = []
    for candidate in boundary_candidates:
        for check in candidate.required_checks:
            if check in {"build", "integration_test"} and check not in required_checks:
                required_checks.append(check)
    return verify_repo(root, required_checks=required_checks, candidates=boundary_candidates)


def _delete_region(lines: list[str], start_line: int, end_line: int) -> list[str]:
    updated = list(lines)
    del updated[start_line - 1 : end_line]
    return updated


def _apply_auto_candidate(lines: list[str], candidate: Candidate) -> tuple[list[str], bool]:
    region = candidate.anchor_regions[0]
    index = region.start_line - 1
    if index < 0 or index >= len(lines):
        return lines, False
    if candidate.kind == "unused_import":
        rewritten = _rewrite_unused_import_line(candidate, lines[index])
        if rewritten is None:
            return lines, False
        updated = list(lines)
        if rewritten == "":
            del updated[index]
        else:
            updated[index] = rewritten
        return updated, updated != lines
    if candidate.kind in {"dead_code", "unused_symbol"}:
        updated = _delete_region(lines, region.start_line, region.end_line)
        return updated, updated != lines
    return lines, False


def _snapshot_repo(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in walk_repo_files(root)}


def _changed_paths(before: dict[str, bytes], after: dict[str, bytes]) -> list[str]:
    return [rel_path for rel_path in sorted(set(before) | set(after)) if before.get(rel_path) != after.get(rel_path)]


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


def _guarded_same_file_diff_reason(
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


def _restore_snapshot(root: Path, snapshot: dict[str, bytes]) -> bool:
    current = _snapshot_repo(root)
    restored = False
    for rel_path in sorted(set(snapshot) | set(current)):
        target = root / rel_path
        original = snapshot.get(rel_path)
        current_bytes = current.get(rel_path)
        if original == current_bytes:
            continue
        if original is None:
            if target.exists():
                target.unlink()
                restored = True
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(original)
        restored = True
    return restored


def _guarded_failure_reason(error: Exception) -> str:
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


def _apply_guarded_candidate(root: Path, candidate: Candidate, guarded_applier: CodexGuardedApplier) -> tuple[bool, str | None]:
    support_reason = guarded_applier.support_reason(root, candidate)
    if support_reason is not None:
        return False, support_reason

    before = _snapshot_repo(root)
    try:
        result = guarded_applier.apply(root, candidate)
    except Exception as exc:  # pragma: no cover
        _restore_snapshot(root, before)
        return False, _guarded_failure_reason(exc)

    after = _snapshot_repo(root)
    changed_paths = _changed_paths(before, after)
    allowed_files = set(candidate.files)
    allowed_candidate_ids = {candidate.id}
    touched_files = set(result.touched_files)
    declared_candidate_ids = set(result.candidate_ids)

    if result.status == "unsupported":
        _restore_snapshot(root, before)
        reason = result.summary[0] if result.summary else "guarded Codex flow reported unsupported"
        return False, reason
    if not declared_candidate_ids:
        _restore_snapshot(root, before)
        return False, "guarded Codex response omitted candidateIds for the selected candidate scope"
    if any(candidate_id not in allowed_candidate_ids for candidate_id in declared_candidate_ids):
        _restore_snapshot(root, before)
        return False, "guarded Codex response declared candidateIds outside the selected candidate scope"

    if any(path not in allowed_files for path in changed_paths):
        _restore_snapshot(root, before)
        return False, "guarded Codex flow touched files outside the allowed candidate scope"
    if any(path not in allowed_files for path in touched_files):
        _restore_snapshot(root, before)
        return False, "guarded Codex response declared files outside the allowed candidate scope"
    if result.status == "no_change":
        if changed_paths:
            _restore_snapshot(root, before)
            return False, "guarded Codex response reported no_change despite modifying the repo"
        if touched_files:
            return False, "guarded Codex response touchedFiles did not match the actual changed files"
        reason = result.summary[0] if result.summary else "guarded Codex flow reported no changes"
        return False, reason
    if touched_files != set(changed_paths):
        _restore_snapshot(root, before)
        return False, "guarded Codex response touchedFiles did not match the actual changed files"
    if not changed_paths:
        return False, "guarded Codex flow produced no file changes"
    diff_reason = _guarded_same_file_diff_reason(before, after, [candidate], "guarded Codex flow")
    if diff_reason is not None:
        _restore_snapshot(root, before)
        return False, diff_reason
    return True, None



def _repair_guarded_changes(
    root: Path,
    guarded_candidates: list[Candidate],
    verification: VerificationResult,
    guarded_applier: CodexGuardedApplier,
) -> _RepairAttempt:
    if not guarded_candidates:
        return _RepairAttempt(RepairResult(status="not_needed", attempted=False, touchedFiles=[]), repaired=False)
    if not guarded_applier.is_available():
        return _RepairAttempt(
            RepairResult(status="skipped", attempted=False, touchedFiles=[], reason="codex cli is not available"),
            repaired=False,
        )

    before = _snapshot_repo(root)
    allowed_files = {file for candidate in guarded_candidates for file in candidate.files}
    allowed_candidate_ids = {candidate.id for candidate in guarded_candidates}
    try:
        result = guarded_applier.repair(root, guarded_candidates, verification)
    except Exception as exc:  # pragma: no cover
        _restore_snapshot(root, before)
        return _RepairAttempt(
            RepairResult(status="failed", attempted=True, touchedFiles=[], reason=_guarded_failure_reason(exc)),
            repaired=False,
        )

    after = _snapshot_repo(root)
    changed_paths = _changed_paths(before, after)
    touched_files = set(result.touched_files)
    declared_candidate_ids = set(result.candidate_ids)

    if result.status == "unsupported":
        _restore_snapshot(root, before)
        return _RepairAttempt(
            RepairResult(
                status="skipped",
                attempted=True,
                touchedFiles=[],
                reason=result.summary[0] if result.summary else "guarded repair is unsupported",
            ),
            repaired=False,
        )
    if not declared_candidate_ids:
        _restore_snapshot(root, before)
        return _RepairAttempt(
            RepairResult(
                status="failed",
                attempted=True,
                touchedFiles=sorted(touched_files),
                reason="guarded Codex repair omitted candidateIds for the selected candidate scope",
            ),
            repaired=False,
        )
    if declared_candidate_ids != allowed_candidate_ids:
        _restore_snapshot(root, before)
        return _RepairAttempt(
            RepairResult(
                status="failed",
                attempted=True,
                touchedFiles=sorted(touched_files),
                reason="guarded Codex repair candidateIds did not match the selected candidate scope",
            ),
            repaired=False,
        )
    if any(path not in allowed_files for path in changed_paths):
        _restore_snapshot(root, before)
        return _RepairAttempt(
            RepairResult(
                status="failed",
                attempted=True,
                touchedFiles=changed_paths,
                reason="guarded Codex repair touched files outside the allowed candidate scope",
            ),
            repaired=False,
        )
    if any(path not in allowed_files for path in touched_files):
        _restore_snapshot(root, before)
        return _RepairAttempt(
            RepairResult(
                status="failed",
                attempted=True,
                touchedFiles=sorted(touched_files),
                reason="guarded Codex repair declared files outside the allowed candidate scope",
            ),
            repaired=False,
        )
    if result.status == "no_change":
        if changed_paths:
            _restore_snapshot(root, before)
            return _RepairAttempt(
                RepairResult(
                    status="failed",
                    attempted=True,
                    touchedFiles=changed_paths,
                    reason="guarded Codex repair reported no_change despite modifying the repo",
                ),
                repaired=False,
            )
        if touched_files:
            return _RepairAttempt(
                RepairResult(
                    status="failed",
                    attempted=True,
                    touchedFiles=sorted(touched_files),
                    reason="guarded Codex repair touchedFiles did not match the actual changed files",
                ),
                repaired=False,
            )
        return _RepairAttempt(
            RepairResult(
                status="skipped",
                attempted=True,
                touchedFiles=[],
                reason=result.summary[0] if result.summary else "guarded Codex repair made no changes",
            ),
            repaired=False,
        )
    if touched_files != set(changed_paths):
        _restore_snapshot(root, before)
        return _RepairAttempt(
            RepairResult(
                status="failed",
                attempted=True,
                touchedFiles=sorted(touched_files),
                reason="guarded Codex repair touchedFiles did not match the actual changed files",
            ),
            repaired=False,
        )
    if not changed_paths:
        return _RepairAttempt(
            RepairResult(status="skipped", attempted=True, touchedFiles=[], reason="guarded Codex repair made no changes"),
            repaired=False,
        )
    diff_reason = _guarded_same_file_diff_reason(before, after, guarded_candidates, "guarded Codex repair")
    if diff_reason is not None:
        _restore_snapshot(root, before)
        return _RepairAttempt(
            RepairResult(status="failed", attempted=True, touchedFiles=changed_paths, reason=diff_reason),
            repaired=False,
        )
    return _RepairAttempt(
        RepairResult(status="repaired", attempted=True, touchedFiles=changed_paths),
        repaired=True,
    )


def _apply_plan_internal(root: Path, plan: PlanResult) -> _ApplyInternalResult:
    applied: list[Candidate] = []
    skipped: list[ExecutionCandidateNote] = []
    file_lines: dict[str, list[str]] = {}
    original_snapshot = _snapshot_repo(root)
    guarded_applier = CodexGuardedApplier()
    changed_files_since_scan: set[str] = set()
    for candidate in sorted(plan.selected_candidates, key=_candidate_sort_key):
        reason = _candidate_support_reason(root, candidate, guarded_applier)
        if candidate.apply_mode_hint == "auto":
            if reason is not None:
                skipped.append(ExecutionCandidateNote(candidate=candidate, reason=reason))
                continue
            rel_path = candidate.files[0]
            if rel_path not in file_lines:
                current_text = (root / rel_path).read_text(encoding="utf-8")
                file_lines[rel_path] = current_text.splitlines(keepends=True)
            updated_lines, changed = _apply_auto_candidate(file_lines[rel_path], candidate)
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
            applied_guarded, reason = _apply_guarded_candidate(root, candidate, guarded_applier)
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
    changed_files = _changed_paths(original_snapshot, _snapshot_repo(root))
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
    verification_plan = VerificationPlanSummary.model_validate(
        build_verification_report(
            root,
            required_checks=plan.required_checks,
            candidates=plan.selected_candidates,
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
    for candidate in plan.selected_candidates:
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

        reason = _candidate_support_reason(root, candidate, guarded_applier)
        if reason is None and boundary_candidate is not None and boundary_candidate.missing_predicates:
            reason = boundary_candidate.blocked_reasons[0] if boundary_candidate.blocked_reasons else boundary_candidate.missing_predicates[0]
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

    if apply_result.status == "no_changes":
        _abort_branch_if_needed(root, git_context)
        verification = VerificationResult(
            status="passed",
            checks=[],
            readiness=VerificationReadiness(
                ready=True,
                proofStatus="not_applicable",
                missingPredicates=[],
                proofRefs=[],
            ),
            proofRecords=[],
        )
        return RunResult(
            mode=plan.mode,
            repo=plan.repo,
            plan=plan,
            apply=ApplyResult.model_validate(apply_result.model_dump(by_alias=True)),
            verification=verification,
            status="no_changes",
            rollbackApplied=False,
            repair=repair_result,
            git=git_result,
        )


    verification = _verify_for_execution(root, plan, apply_result.applied_candidates)
    guarded_candidates = [candidate for candidate in apply_result.applied_candidates if candidate.apply_mode_hint == "guarded"]
    if verification.status == "failed":
        repair_attempt = _repair_guarded_changes(root, guarded_candidates, verification, CodexGuardedApplier())
        repair_result = repair_attempt.result
        if repair_attempt.repaired:
            verification = _verify_for_execution(root, plan, apply_result.applied_candidates)

    if verification.status == "failed":
        rollback_applied = _restore_snapshot(root, apply_result.original_snapshot)
        _abort_branch_if_needed(root, git_context)
        return RunResult(
            mode=plan.mode,
            repo=plan.repo,
            plan=plan,
            apply=ApplyResult.model_validate(apply_result.model_dump(by_alias=True)),
            verification=verification,
            status="rolled_back",
            rollbackApplied=rollback_applied,
            repair=repair_result,
            git=git_result,
        )

    if git_context is not None:
        try:
            git_result.commit_sha = finalize_git_execution(root, git_context, apply_result.changed_files, plan.mode)
        except subprocess.CalledProcessError:
            rollback_applied = _restore_snapshot(root, apply_result.original_snapshot)
            _abort_branch_if_needed(root, git_context)
            failed_verification = verification.model_copy(update={"status": "failed"})
            return RunResult(
                mode=plan.mode,
                repo=plan.repo,
                plan=plan,
                apply=ApplyResult.model_validate(apply_result.model_dump(by_alias=True)),
                verification=failed_verification,
                status="rolled_back",
                rollbackApplied=rollback_applied,
                repair=repair_result,
                git=GitExecutionResult(
                    enabled=git_result.enabled,
                    available=git_result.available,
                    clean=git_result.clean,
                    baseBranch=git_result.base_branch,
                    executionBranch=git_result.execution_branch,
                    reason="git commit failed after successful verification",
                ),
            )

    return RunResult(
        mode=plan.mode,
        repo=plan.repo,
        plan=plan,
        apply=ApplyResult.model_validate(apply_result.model_dump(by_alias=True)),
        verification=verification,
        status="passed",
        rollbackApplied=False,
        repair=repair_result,
        git=git_result,
    )
