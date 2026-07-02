from __future__ import annotations

import ast
from pathlib import Path

from refactorq.adapters.python.builders import (
    build_duplicate_candidates,
    build_inline_function_candidates,
    build_remove_abstraction_candidates,
)
from refactorq.adapters.python.common import (
    LARGE_MODULE_THRESHOLD,
    TOP_LEVEL_STATEMENT_THRESHOLD,
    benefit,
    diff,
    region,
    risk,
    unused_import_apply_mode,
)
from refactorq.adapters.python.function_candidates import (
    collect_function_candidates,
    collect_name_context,
    dead_code_candidates,
)
from refactorq.adapters.python.graph import module_name, path_layer, resolve_import_targets
from refactorq.adapters.python.layer_candidates import layer_boundary_candidates
from refactorq.core.candidate.models import Candidate, Provenance


def scan_file(root: Path, path: Path, known_modules: dict[str, str]) -> tuple[list[Candidate], set[str]]:
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return [], set()

    rel_path = path.relative_to(root).as_posix()
    lines = source.splitlines()
    raw_lines = source.splitlines(keepends=True)
    loaded_name_counts, referenced_names, exported_names = collect_name_context(tree)
    current_module = module_name(root, path)
    is_package = path.name == "__init__.py"
    imports: set[str] = set()
    candidates: list[Candidate] = []
    layer_violations: list[tuple[int, str, tuple[str, ...]]] = []
    seen_layer_violations: set[tuple[int, str, tuple[str, ...]]] = set()
    duplicate_functions: list[tuple[str, int, int, str]] = []
    passthrough_functions: list[tuple[str, int, int, str]] = []
    inline_functions: list[tuple[str, int, int, int]] = []

    _add_large_module_candidate(candidates, rel_path, lines, len(tree.body))

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            _collect_import_candidates(
                root,
                path,
                known_modules,
                rel_path,
                raw_lines,
                referenced_names,
                current_module,
                is_package,
                imports,
                candidates,
                layer_violations,
                seen_layer_violations,
                node,
            )

        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.end_lineno is not None:
            collect_function_candidates(
                rel_path,
                lines,
                loaded_name_counts,
                exported_names,
                candidates,
                duplicate_functions,
                passthrough_functions,
                inline_functions,
                node,
            )

    candidates.extend(dead_code_candidates(rel_path, tree, referenced_names, exported_names))
    candidates.extend(build_duplicate_candidates(rel_path, duplicate_functions))
    candidates.extend(build_remove_abstraction_candidates(rel_path, passthrough_functions))
    candidates.extend(build_inline_function_candidates(rel_path, inline_functions))
    candidates.extend(layer_boundary_candidates(rel_path, layer_violations))
    return candidates, imports


def _add_large_module_candidate(
    candidates: list[Candidate], rel_path: str, lines: list[str], top_level_statements: int
) -> None:
    if len(lines) < LARGE_MODULE_THRESHOLD and top_level_statements < TOP_LEVEL_STATEMENT_THRESHOLD:
        return
    candidates.append(
        Candidate(
            id=f"py-split-large-module-{rel_path}",
            kind="split_large_module",
            title=f"Split large module {rel_path}",
            description=(
                f"Module `{rel_path}` spans {len(lines)} lines across {top_level_statements} top-level"
                " statements and should be reviewed for decomposition"
            ),
            language="python",
            scope="module",
            source=["metric"],
            files=[rel_path],
            estimatedBenefit=benefit(
                {"complexityReduction": min(1.0, len(lines) / LARGE_MODULE_THRESHOLD), "maintainabilityGain": 0.4}
            ),
            estimatedRisk=risk({"semanticRisk": 0.45, "apiRisk": 0.25, "testRisk": 0.3, "conflictRisk": 0.2}),
            estimatedDiff=diff({"filesTouched": 1, "linesAdded": max(8, len(lines) // 5), "linesModified": len(lines)}),
            confidence=0.7,
            applyModeHint="report_only",
            requiredChecks=["parse", "lint", "typecheck", "unit_test"],
            provenance=Provenance(
                detectors=["python-ast-large-module"],
                evidence=[f"line_span:{len(lines)}", f"top_level_statements:{top_level_statements}"],
            ),
        )
    )


def _collect_import_candidates(
    root: Path,
    path: Path,
    known_modules: dict[str, str],
    rel_path: str,
    raw_lines: list[str],
    referenced_names: set[str],
    current_module: str,
    is_package: bool,
    imports: set[str],
    candidates: list[Candidate],
    layer_violations: list[tuple[int, str, tuple[str, ...]]],
    seen_layer_violations: set[tuple[int, str, tuple[str, ...]]],
    node: ast.Import | ast.ImportFrom,
) -> None:
    for alias in node.names:
        bound_name = alias.asname or alias.name.split(".")[0]
        if is_package or bound_name in referenced_names:
            continue
        candidates.append(_unused_import_candidate(rel_path, raw_lines, node, bound_name))

    resolved_targets = resolve_import_targets(current_module, is_package, node, known_modules)
    current_layer = path_layer(rel_path)
    imported_symbols = tuple(
        sorted(bound_name for alias in node.names if (bound_name := (alias.asname or alias.name.split(".")[0])) != "*")
    )
    for target_module in resolved_targets:
        target_rel_path = known_modules.get(target_module)
        target_layer = path_layer(target_rel_path) if target_rel_path else None
        if current_layer and target_rel_path and target_layer and current_layer != target_layer:
            violation = (node.lineno, target_rel_path, imported_symbols)
            if violation not in seen_layer_violations:
                seen_layer_violations.add(violation)
                layer_violations.append(violation)
    imports.update(resolved_targets)


def _unused_import_candidate(
    rel_path: str,
    raw_lines: list[str],
    node: ast.Import | ast.ImportFrom,
    bound_name: str,
) -> Candidate:
    return Candidate(
        id=f"py-unused-import-{rel_path}-{node.lineno}-{bound_name}",
        kind="unused_import",
        title=f"Remove unused import {bound_name}",
        description=f"Unused Python import `{bound_name}` in {rel_path}",
        language="python",
        scope="local",
        source=["static"],
        files=[rel_path],
        symbols=[bound_name],
        anchorRegions=[region(rel_path, node.lineno, node.end_lineno or node.lineno)],
        estimatedBenefit=benefit({"maintainabilityGain": 0.08}),
        estimatedRisk=risk({"semanticRisk": 0.02, "conflictRisk": 0.03}),
        estimatedDiff=diff({"filesTouched": 1, "linesDeleted": 1, "linesModified": 1}),
        confidence=0.95,
        applyModeHint=unused_import_apply_mode(raw_lines, node, bound_name),
        requiredChecks=["parse", "lint", "typecheck"],
        provenance=Provenance(
            detectors=["python-ast-unused-import"],
            evidence=[f"line:{node.lineno}", f"symbol:{bound_name}"],
        ),
    )
