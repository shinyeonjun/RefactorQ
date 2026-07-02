from __future__ import annotations

import re
from pathlib import Path

from refactorq.core.candidate import Candidate


_IMPORT_PREFIXES = ("import ", "from ")
_TS_IMPORT_PATTERN = re.compile(
    r'^(?P<indent>\s*)import\s+(?P<clause>.+?)\s+from\s+(?P<source>["\'][^"\']+["\'];?\s*)$'
)


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
    return specifier


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


def auto_support_reason(root: Path, candidate: Candidate) -> str | None:
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


def _delete_region(lines: list[str], start_line: int, end_line: int) -> list[str]:
    updated = list(lines)
    del updated[start_line - 1 : end_line]
    return updated


def apply_auto_candidate(lines: list[str], candidate: Candidate) -> tuple[list[str], bool]:
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
