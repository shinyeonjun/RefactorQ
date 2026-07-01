from __future__ import annotations

import re
from pathlib import Path

from refactorq.core.candidate import Candidate
from refactorq.core.planning import PlanResult
from refactorq.core.verification import VerificationResult
from refactorq.core.verification.service import verify_repo

from .models import ApplyResult, ExecutionCandidateNote, ExecutionSupportSummary, ReportResult, RunResult

_IMPORT_PREFIXES = ("import ", "from ")
_TS_IMPORT_PATTERN = re.compile(
    r'^(?P<indent>\s*)import\s+(?P<clause>.+?)\s+from\s+(?P<source>["\'][^"\']+["\'];?\s*)$'
)


class _ApplyInternalResult(ApplyResult):
    backups: dict[str, str] = {}


def _candidate_line_number(candidate: Candidate) -> int:
    if not candidate.anchor_regions:
        return 0
    return candidate.anchor_regions[0].start_line


def _candidate_sort_key(candidate: Candidate) -> tuple[str, int, str]:
    file_name = candidate.files[0] if candidate.files else ""
    return (file_name, -_candidate_line_number(candidate), candidate.id)


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


def _support_reason(root: Path, candidate: Candidate) -> str | None:
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


def _apply_candidate(lines: list[str], candidate: Candidate) -> tuple[list[str], bool]:
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


def _apply_plan_internal(root: Path, plan: PlanResult) -> _ApplyInternalResult:
    applied: list[Candidate] = []
    skipped: list[ExecutionCandidateNote] = []
    file_lines: dict[str, list[str]] = {}
    backups: dict[str, str] = {}

    for candidate in sorted(plan.selected_candidates, key=_candidate_sort_key):
        reason = _support_reason(root, candidate)
        if reason is not None:
            skipped.append(ExecutionCandidateNote(candidate=candidate, reason=reason))
            continue
        rel_path = candidate.files[0]
        if rel_path not in backups:
            original_text = (root / rel_path).read_text(encoding="utf-8")
            backups[rel_path] = original_text
            file_lines[rel_path] = original_text.splitlines(keepends=True)
        updated_lines, changed = _apply_candidate(file_lines[rel_path], candidate)
        if not changed:
            skipped.append(
                ExecutionCandidateNote(candidate=candidate, reason="candidate produced no deterministic file change")
            )
            continue
        file_lines[rel_path] = updated_lines
        applied.append(candidate)

    changed_files: list[str] = []
    for rel_path, original_text in backups.items():
        updated_text = "".join(file_lines[rel_path])
        if updated_text == original_text:
            continue
        (root / rel_path).write_text(updated_text, encoding="utf-8")
        changed_files.append(rel_path)

    return _ApplyInternalResult(
        mode=plan.mode,
        repo=plan.repo,
        plan=plan,
        status="applied" if changed_files else "no_changes",
        appliedCandidates=applied,
        skippedCandidates=skipped,
        changedFiles=sorted(changed_files),
        backups=backups,
    )


def _restore_backups(root: Path, backups: dict[str, str]) -> bool:
    restored = False
    for rel_path, original_text in backups.items():
        target = root / rel_path
        if target.read_text(encoding="utf-8") == original_text:
            continue
        target.write_text(original_text, encoding="utf-8")
        restored = True
    return restored


def apply_plan(root: Path, plan: PlanResult) -> ApplyResult:
    result = _apply_plan_internal(root, plan)
    return ApplyResult.model_validate(result.model_dump(by_alias=True))


def report_plan(root: Path, plan: PlanResult) -> ReportResult:
    supported = 0
    unsupported = 0
    kinds: set[str] = set()
    for candidate in plan.selected_candidates:
        if _support_reason(root, candidate) is None:
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
        rollback_applied = _restore_backups(root, apply_result.backups)
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
