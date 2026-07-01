from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from pydantic import ValidationError

from refactorq.agents.codex import CodexGuardedApplier
from refactorq.core.candidate import Candidate
from refactorq.core.filesystem import walk_repo_files
from refactorq.core.planning import PlanResult
from refactorq.core.verification import VerificationResult
from refactorq.core.verification.service import verify_repo

from .models import ApplyResult, ExecutionCandidateNote, ExecutionSupportSummary, ReportResult, RunResult

_IMPORT_PREFIXES = ("import ", "from ")
_TS_IMPORT_PATTERN = re.compile(
    r'^(?P<indent>\s*)import\s+(?P<clause>.+?)\s+from\s+(?P<source>["\'][^"\']+["\'];?\s*)$'
)


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
    if default_part is not None and named_part is None:
        return f"{indent}import {default_part} from {source}{newline}"
    if default_part is None and not named_specifiers:
        return ""
    if default_part is None:
        return f"{indent}import {{ {', '.join(named_specifiers)} }} from {source}{newline}"
    if not named_specifiers:
        return f"{indent}import {default_part} from {source}{newline}"
    return f"{indent}import {default_part}, {{ {', '.join(named_specifiers)} }} from {source}{newline}"


def _rewrite_unused_import_line(candidate: Candidate, line: str) -> str | None:
    if candidate.language == "python":
        return _rewrite_python_import(line, candidate.symbols[0])
    if candidate.language in {"typescript", "javascript"}:
        return _rewrite_typescript_import(line, candidate.symbols[0])
    return None


def _auto_support_reason(root: Path, candidate: Candidate) -> str | None:
    if candidate.apply_mode_hint != "auto":
        return "candidate requires guarded handling"
    if candidate.kind != "unused_import":
        return "deterministic patcher currently supports unused_import only"
    if len(candidate.files) != 1 or len(candidate.anchor_regions) != 1 or len(candidate.symbols) != 1:
        return "candidate does not target a single file, region, and symbol"
    region = candidate.anchor_regions[0]
    if region.start_line != region.end_line:
        return "candidate spans multiple lines"
    target = root / candidate.files[0]
    if not target.exists():
        return "candidate target file is missing"
    lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
    if region.start_line < 1 or region.start_line > len(lines):
        return "candidate line falls outside the current file"
    line = lines[region.start_line - 1]
    if not _looks_like_single_line_import(line):
        return "candidate import statement is not a supported single-line import"
    if _rewrite_unused_import_line(candidate, line) is None:
        return "candidate import statement cannot be rewritten deterministically"
    return None


def _apply_auto_candidate(lines: list[str], candidate: Candidate) -> tuple[list[str], bool]:
    region = candidate.anchor_regions[0]
    index = region.start_line - 1
    if index < 0 or index >= len(lines):
        return lines, False
    rewritten = _rewrite_unused_import_line(candidate, lines[index])
    if rewritten is None:
        return lines, False
    updated = list(lines)
    if rewritten == "":
        del updated[index]
    else:
        updated[index] = rewritten
    return updated, updated != lines


def _snapshot_repo(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in walk_repo_files(root)}


def _changed_paths(before: dict[str, bytes], after: dict[str, bytes]) -> list[str]:
    changed: list[str] = []
    for rel_path in sorted(set(before) | set(after)):
        if before.get(rel_path) != after.get(rel_path):
            changed.append(rel_path)
    return changed


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


def _apply_guarded_candidate(
    root: Path,
    candidate: Candidate,
    guarded_applier: CodexGuardedApplier,
) -> tuple[bool, str | None]:
    support_reason = guarded_applier.support_reason(root, candidate)
    if support_reason is not None:
        return False, support_reason

    before = _snapshot_repo(root)
    try:
        result = guarded_applier.apply(root, candidate)
    except Exception as exc:  # pragma: no cover - exercised through failure handling tests
        _restore_snapshot(root, before)
        return False, _guarded_failure_reason(exc)

    after = _snapshot_repo(root)
    changed_paths = _changed_paths(before, after)
    allowed_files = set(candidate.files)
    touched_files = set(result.touched_files)

    if result.status == "unsupported":
        _restore_snapshot(root, before)
        reason = result.summary[0] if result.summary else "guarded Codex flow reported unsupported"
        return False, reason
    if result.status == "no_change" and not changed_paths:
        reason = result.summary[0] if result.summary else "guarded Codex flow reported no changes"
        return False, reason
    if any(path not in allowed_files for path in changed_paths):
        _restore_snapshot(root, before)
        return False, "guarded Codex flow touched files outside the allowed candidate scope"
    if any(path not in allowed_files for path in touched_files):
        _restore_snapshot(root, before)
        return False, "guarded Codex response declared files outside the allowed candidate scope"
    if not changed_paths:
        return False, "guarded Codex flow produced no file changes"
    return True, None


def _apply_plan_internal(root: Path, plan: PlanResult) -> _ApplyInternalResult:
    applied: list[Candidate] = []
    skipped: list[ExecutionCandidateNote] = []
    file_lines: dict[str, list[str]] = {}
    original_snapshot = _snapshot_repo(root)
    guarded_applier = CodexGuardedApplier()

    for candidate in sorted(plan.selected_candidates, key=_candidate_sort_key):
        if candidate.apply_mode_hint == "auto":
            reason = _auto_support_reason(root, candidate)
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
            continue

        if candidate.apply_mode_hint == "guarded":
            applied_guarded, reason = _apply_guarded_candidate(root, candidate, guarded_applier)
            if not applied_guarded:
                skipped.append(
                    ExecutionCandidateNote(candidate=candidate, reason=reason or "guarded Codex flow skipped candidate")
                )
                continue
            applied.append(candidate)
            continue

        skipped.append(ExecutionCandidateNote(candidate=candidate, reason="report-only candidate is not applied"))

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


def _supports_candidate(root: Path, candidate: Candidate, guarded_applier: CodexGuardedApplier) -> bool:
    if candidate.apply_mode_hint == "auto":
        return _auto_support_reason(root, candidate) is None
    if candidate.apply_mode_hint == "guarded":
        return guarded_applier.support_reason(root, candidate) is None
    return False


def report_plan(root: Path, plan: PlanResult) -> ReportResult:
    supported = 0
    unsupported = 0
    kinds: set[str] = set()
    guarded_applier = CodexGuardedApplier()
    for candidate in plan.selected_candidates:
        if _supports_candidate(root, candidate, guarded_applier):
            supported += 1
            kinds.add(candidate.kind)
        else:
            unsupported += 1
    return ReportResult(
        mode=plan.mode,
        repo=plan.repo,
        plan=plan,
        executionSupport=ExecutionSupportSummary(
            supportedCandidates=supported,
            unsupportedCandidates=unsupported,
            appliedCandidateKinds=sorted(kinds),
        ),
    )


def run_plan(root: Path, plan: PlanResult) -> RunResult:
    apply_result = _apply_plan_internal(root, plan)
    verification: VerificationResult
    rollback_applied = False
    if apply_result.status == "no_changes":
        verification = VerificationResult(status="passed", checks=[])
        return RunResult(
            mode=plan.mode,
            repo=plan.repo,
            plan=plan,
            apply=ApplyResult.model_validate(apply_result.model_dump(by_alias=True)),
            verification=verification,
            status="no_changes",
            rollbackApplied=False,
        )

    verification = verify_repo(root)
    if verification.status == "failed":
        rollback_applied = _restore_snapshot(root, apply_result.original_snapshot)
        return RunResult(
            mode=plan.mode,
            repo=plan.repo,
            plan=plan,
            apply=ApplyResult.model_validate(apply_result.model_dump(by_alias=True)),
            verification=verification,
            status="rolled_back",
            rollbackApplied=rollback_applied,
        )

    return RunResult(
        mode=plan.mode,
        repo=plan.repo,
        plan=plan,
        apply=ApplyResult.model_validate(apply_result.model_dump(by_alias=True)),
        verification=verification,
        status="passed",
        rollbackApplied=False,
    )
