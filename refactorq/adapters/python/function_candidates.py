from __future__ import annotations

import ast

from refactorq.adapters.python.builders import top_level_unused_assignment_candidate
from refactorq.adapters.python.common import (
    INLINE_FUNCTION_MAX_LINES,
    LONG_FUNCTION_THRESHOLD,
    benefit,
    diff,
    duplicate_function_key,
    exported_names as collect_exported_names,
    loaded_name_counts as collect_loaded_name_counts,
    passthrough_target,
    region,
    risk,
)
from refactorq.core.candidate.models import Candidate, Provenance


def collect_name_context(tree: ast.Module) -> tuple[dict[str, int], set[str], set[str]]:
    loaded_name_counts = collect_loaded_name_counts(tree)
    return loaded_name_counts, set(loaded_name_counts), collect_exported_names(tree)


def collect_function_candidates(
    rel_path: str,
    lines: list[str],
    loaded_name_counts: dict[str, int],
    exported_names: set[str],
    candidates: list[Candidate],
    duplicate_functions: list[tuple[str, int, int, str]],
    passthrough_functions: list[tuple[str, int, int, str]],
    inline_functions: list[tuple[str, int, int, int]],
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> None:
    end_line = node.end_lineno
    assert end_line is not None
    length = end_line - node.lineno + 1
    duplicate_functions.append((node.name, node.lineno, end_line, duplicate_function_key(node)))
    target_name = passthrough_target(node)
    is_private = node.name.startswith("_") and not node.name.startswith("__") and node.name not in exported_names
    if target_name and is_private:
        passthrough_functions.append((node.name, node.lineno, end_line, target_name))
    if (
        loaded_name_counts.get(node.name, 0) == 1
        and is_private
        and target_name is None
        and not node.decorator_list
        and length <= INLINE_FUNCTION_MAX_LINES
    ):
        inline_functions.append((node.name, node.lineno, end_line, 1))
    if length < LONG_FUNCTION_THRESHOLD:
        return
    candidates.append(
        Candidate(
            id=f"py-extract-function-{rel_path}-{node.lineno}-{node.name}",
            kind="extract_function",
            title=f"Extract logic from long function {node.name}",
            description=(
                f"Function `{node.name}` in {rel_path} spans {length} lines and is a candidate for extraction"
            ),
            language="python",
            scope="local",
            source=["static", "metric"],
            files=[rel_path],
            symbols=[node.name],
            anchorRegions=[region(rel_path, node.lineno, end_line)],
            estimatedBenefit=benefit(
                {
                    "complexityReduction": min(1.0, length / max(len(lines), 1)),
                    "maintainabilityGain": 0.35,
                }
            ),
            estimatedRisk=risk({"semanticRisk": 0.35, "testRisk": 0.25, "conflictRisk": 0.15}),
            estimatedDiff=diff({"filesTouched": 1, "linesAdded": max(3, length // 4), "linesModified": length}),
            confidence=0.72,
            applyModeHint="guarded",
            requiredChecks=["parse", "lint", "typecheck", "unit_test"],
            provenance=Provenance(
                detectors=["python-ast-long-function"],
                evidence=[f"line_span:{length}", f"symbol:{node.name}"],
            ),
        )
    )


def dead_code_candidates(
    rel_path: str,
    tree: ast.Module,
    referenced_names: set[str],
    exported_names: set[str],
) -> list[Candidate]:
    candidates: list[Candidate] = []
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            candidate = top_level_unused_assignment_candidate(rel_path, node, referenced_names, exported_names)
            if candidate is not None:
                candidates.append(candidate)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.end_lineno is not None:
            if _is_dead_private_symbol(node.name, referenced_names, exported_names):
                candidates.append(_dead_function_candidate(rel_path, node))
        elif isinstance(node, ast.ClassDef) and node.end_lineno is not None:
            if _is_dead_private_symbol(node.name, referenced_names, exported_names):
                candidates.append(_dead_class_candidate(rel_path, node))
    return candidates


def _is_dead_private_symbol(name: str, referenced_names: set[str], exported_names: set[str]) -> bool:
    return name.startswith("_") and not name.startswith("__") and name not in referenced_names and name not in exported_names


def _dead_function_candidate(rel_path: str, node: ast.FunctionDef | ast.AsyncFunctionDef) -> Candidate:
    end_line = node.end_lineno
    assert end_line is not None
    length = end_line - node.lineno + 1
    return Candidate(
        id=f"py-dead-code-{rel_path}-{node.lineno}-{node.name}",
        kind="dead_code",
        title=f"Remove unused private function {node.name}",
        description=f"Top-level private function `{node.name}` in {rel_path} is not referenced",
        language="python",
        scope="module",
        source=["static"],
        files=[rel_path],
        symbols=[node.name],
        anchorRegions=[region(rel_path, node.lineno, end_line)],
        estimatedBenefit=benefit({"maintainabilityGain": 0.18}),
        estimatedRisk=risk({"semanticRisk": 0.08, "conflictRisk": 0.04}),
        estimatedDiff=diff({"filesTouched": 1, "linesDeleted": length, "linesModified": length}),
        confidence=0.86,
        applyModeHint="auto",
        requiredChecks=["parse", "lint", "typecheck"],
        provenance=Provenance(
            detectors=["python-ast-dead-code"],
            evidence=[f"line_span:{length}", f"symbol:{node.name}"],
        ),
    )


def _dead_class_candidate(rel_path: str, node: ast.ClassDef) -> Candidate:
    end_line = node.end_lineno
    assert end_line is not None
    length = end_line - node.lineno + 1
    return Candidate(
        id=f"py-dead-code-{rel_path}-{node.lineno}-{node.name}",
        kind="dead_code",
        title=f"Remove unused private class {node.name}",
        description=f"Top-level private class `{node.name}` in {rel_path} is not referenced",
        language="python",
        scope="module",
        source=["static"],
        files=[rel_path],
        symbols=[node.name],
        anchorRegions=[region(rel_path, node.lineno, end_line)],
        estimatedBenefit=benefit({"maintainabilityGain": 0.18}),
        estimatedRisk=risk({"semanticRisk": 0.1, "conflictRisk": 0.05}),
        estimatedDiff=diff({"filesTouched": 1, "linesDeleted": length, "linesModified": length}),
        confidence=0.84,
        applyModeHint="auto",
        requiredChecks=["parse", "lint", "typecheck"],
        provenance=Provenance(
            detectors=["python-ast-dead-code"],
            evidence=[f"line_span:{length}", f"symbol:{node.name}"],
        ),
    )
