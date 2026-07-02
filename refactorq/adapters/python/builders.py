from __future__ import annotations

from collections import defaultdict
import ast

from refactorq.adapters.python.common import (
    DUPLICATE_FUNCTION_MIN_LINES,
    INLINE_FUNCTION_MAX_LINES,
    LONG_FUNCTION_THRESHOLD,
    benefit,
    diff,
    is_private_unexported_name,
    is_side_effect_free_python_initializer,
    region,
    risk,
)
from refactorq.core.candidate.models import Candidate, Provenance


def top_level_unused_assignment_candidate(
    rel_path: str,
    node: ast.Assign | ast.AnnAssign,
    referenced_names: set[str],
    exported_names: set[str],
) -> Candidate | None:
    if node.end_lineno is None:
        return None
    target: ast.expr
    value: ast.expr | None
    if isinstance(node, ast.Assign):
        if len(node.targets) != 1:
            return None
        target = node.targets[0]
        value = node.value
    else:
        if not node.simple:
            return None
        target = node.target
        value = node.value
    if value is None:
        return None
    if not isinstance(target, ast.Name):
        return None
    if not is_private_unexported_name(target.id, exported_names):
        return None
    if target.id in referenced_names:
        return None
    if not is_side_effect_free_python_initializer(value):
        return None
    length = node.end_lineno - node.lineno + 1
    return Candidate(
        id=f"py-unused-symbol-{rel_path}-{node.lineno}-{target.id}",
        kind="unused_symbol",
        title=f"Remove unused private assignment {target.id}",
        description=f"Top-level private assignment `{target.id}` in {rel_path} is not referenced",
        language="python",
        scope="module",
        source=["static"],
        files=[rel_path],
        symbols=[target.id],
        anchorRegions=[region(rel_path, node.lineno, node.end_lineno)],
        estimatedBenefit=benefit({"maintainabilityGain": 0.12}),
        estimatedRisk=risk({"semanticRisk": 0.03, "conflictRisk": 0.03}),
        estimatedDiff=diff(
            {
                "filesTouched": 1,
                "linesDeleted": length,
                "linesModified": length,
            }
        ),
        confidence=0.9,
        applyModeHint="auto",
        requiredChecks=["parse", "lint", "typecheck"],
        provenance=Provenance(
            detectors=["python-ast-unused-symbol"],
            evidence=[f"line_span:{length}", f"symbol:{target.id}"],
        ),
    )


def build_inline_function_candidates(
    rel_path: str,
    inline_functions: list[tuple[str, int, int, int]],
) -> list[Candidate]:
    candidates: list[Candidate] = []
    for name, start_line, end_line, reference_count in inline_functions:
        length = end_line - start_line + 1
        candidates.append(
            Candidate(
                id=f"py-inline-function-{rel_path}-{start_line}-{name}",
                kind="inline_function",
                title=f"Inline single-use helper {name}",
                description=(
                    f"Private helper `{name}` in {rel_path} is referenced only once and is a candidate for"
                    " inlining into its caller"
                ),
                language="python",
                scope="module",
                source=["static", "metric"],
                files=[rel_path],
                symbols=[name],
                anchorRegions=[region(rel_path, start_line, end_line)],
                estimatedBenefit=benefit(
                    {
                        "complexityReduction": min(1.0, length / max(INLINE_FUNCTION_MAX_LINES, 1)),
                        "maintainabilityGain": 0.24,
                    }
                ),
                estimatedRisk=risk(
                    {
                        "semanticRisk": 0.24,
                        "apiRisk": 0.06,
                        "testRisk": 0.18,
                        "conflictRisk": 0.1,
                    }
                ),
                estimatedDiff=diff(
                    {
                        "filesTouched": 1,
                        "linesAdded": max(1, length // 2),
                        "linesModified": length,
                    }
                ),
                confidence=0.72,
                applyModeHint="guarded",
                requiredChecks=["parse", "lint", "typecheck", "unit_test"],
                provenance=Provenance(
                    detectors=["python-ast-single-use-helper"],
                    evidence=[f"symbol:{name}", f"referenceCount:{reference_count}", f"line_span:{length}"],
                ),
            )
        )
    return candidates


def build_duplicate_candidates(
    rel_path: str,
    duplicates: list[tuple[str, int, int, str]],
) -> list[Candidate]:
    by_key: dict[str, list[tuple[str, int, int]]] = defaultdict(list)
    for name, start_line, end_line, duplicate_key in duplicates:
        by_key[duplicate_key].append((name, start_line, end_line))

    candidates: list[Candidate] = []
    for duplicate_key, functions in sorted(by_key.items()):
        if len(functions) < 2:
            continue
        line_spans = [end_line - start_line + 1 for _, start_line, end_line in functions]
        if max(line_spans) < DUPLICATE_FUNCTION_MIN_LINES:
            continue
        symbols = [name for name, _, _ in functions]
        anchor_regions = [region(rel_path, start_line, end_line) for _, start_line, end_line in functions]
        total_lines = sum(line_spans)
        start_line = min(start for _, start, _ in functions)
        candidates.append(
            Candidate(
                id=f"py-duplicate-logic-{rel_path}-{start_line}-{len(functions)}",
                kind="duplicate_logic",
                title=f"Consolidate duplicate Python functions in {rel_path}",
                description=(
                    f"Functions {', '.join(f'`{name}`' for name in symbols)} in {rel_path} share the same"
                    " structure and are candidates for consolidation"
                ),
                language="python",
                scope="module",
                source=["clone", "metric"],
                files=[rel_path],
                symbols=symbols,
                anchorRegions=anchor_regions,
                estimatedBenefit=benefit(
                    {
                        "duplicationReduction": min(1.0, len(functions) / 3),
                        "maintainabilityGain": 0.42,
                    }
                ),
                estimatedRisk=risk(
                    {
                        "semanticRisk": 0.28,
                        "apiRisk": 0.12,
                        "testRisk": 0.22,
                        "conflictRisk": 0.18,
                    }
                ),
                estimatedDiff=diff(
                    {
                        "filesTouched": 1,
                        "linesAdded": max(3, total_lines // 6),
                        "linesModified": total_lines,
                    }
                ),
                confidence=0.76,
                applyModeHint="guarded",
                requiredChecks=["parse", "lint", "typecheck", "unit_test"],
                provenance=Provenance(
                    detectors=["python-ast-duplicate-function"],
                    evidence=[f"symbol:{name}" for name in symbols] + [f"duplicateGroupSize:{len(functions)}"],
                ),
            )
        )
    return candidates


def build_remove_abstraction_candidates(
    rel_path: str,
    passthrough_functions: list[tuple[str, int, int, str]],
) -> list[Candidate]:
    candidates: list[Candidate] = []
    for name, start_line, end_line, target_name in passthrough_functions:
        length = end_line - start_line + 1
        candidates.append(
            Candidate(
                id=f"py-remove-abstraction-{rel_path}-{start_line}-{name}",
                kind="remove_abstraction",
                title=f"Inline thin wrapper {name}",
                description=(
                    f"Private wrapper `{name}` in {rel_path} only forwards to `{target_name}` and is a"
                    " candidate for inlining or removal"
                ),
                language="python",
                scope="module",
                source=["static", "metric"],
                files=[rel_path],
                symbols=[name],
                anchorRegions=[region(rel_path, start_line, end_line)],
                estimatedBenefit=benefit(
                    {
                        "complexityReduction": min(1.0, length / max(LONG_FUNCTION_THRESHOLD, 1)),
                        "maintainabilityGain": 0.26,
                    }
                ),
                estimatedRisk=risk(
                    {
                        "semanticRisk": 0.2,
                        "apiRisk": 0.08,
                        "testRisk": 0.18,
                        "conflictRisk": 0.12,
                    }
                ),
                estimatedDiff=diff(
                    {
                        "filesTouched": 1,
                        "linesAdded": max(1, length // 3),
                        "linesModified": length,
                    }
                ),
                confidence=0.74,
                applyModeHint="guarded",
                requiredChecks=["parse", "lint", "typecheck", "unit_test"],
                provenance=Provenance(
                    detectors=["python-ast-passthrough-wrapper"],
                    evidence=[f"symbol:{name}", f"target:{target_name}", f"line_span:{length}"],
                ),
            )
        )
    return candidates
