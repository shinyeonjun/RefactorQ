from __future__ import annotations

from collections import defaultdict
import ast
from pathlib import Path

from refactorq.core.candidate.models import (
    AnchorRegion,
    Candidate,
    EstimatedBenefit,
    EstimatedDiff,
    EstimatedRisk,
    Provenance,
)
from refactorq.core.filesystem import walk_source_files


LONG_FUNCTION_THRESHOLD = 35
LARGE_MODULE_THRESHOLD = 300
TOP_LEVEL_STATEMENT_THRESHOLD = 18
DUPLICATE_FUNCTION_MIN_LINES = 3


def _region(file: str, start_line: int, end_line: int) -> AnchorRegion:
    return AnchorRegion.model_validate({"file": file, "startLine": start_line, "endLine": end_line})


def _benefit(payload: dict[str, float]) -> EstimatedBenefit:
    return EstimatedBenefit.model_validate(payload)


def _risk(payload: dict[str, float]) -> EstimatedRisk:
    return EstimatedRisk.model_validate(payload)


def _diff(payload: dict[str, int]) -> EstimatedDiff:
    return EstimatedDiff.model_validate(payload)


def _duplicate_function_key(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    args_dump = ast.dump(node.args, annotate_fields=False, include_attributes=False)
    body_dump = ast.dump(ast.Module(body=node.body, type_ignores=[]), annotate_fields=False, include_attributes=False)
    return f"{args_dump}|{body_dump}"


def _passthrough_target(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    if node.decorator_list or len(node.body) != 1:
        return None
    if node.args.vararg or node.args.kwarg or node.args.kwonlyargs:
        return None
    statement = node.body[0]
    if not isinstance(statement, ast.Return) or statement.value is None:
        return None
    value = statement.value
    if isinstance(value, ast.Await):
        value = value.value
    if not isinstance(value, ast.Call) or value.keywords:
        return None
    if len(value.args) != len(node.args.args):
        return None
    parameter_names = [argument.arg for argument in node.args.args]
    forwarded_names: list[str] = []
    for argument in value.args:
        if not isinstance(argument, ast.Name):
            return None
        forwarded_names.append(argument.id)
    if forwarded_names != parameter_names:
        return None
    if not isinstance(value.func, ast.Name) or value.func.id == node.name:
        return None
    return value.func.id


def _build_duplicate_candidates(
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
        anchor_regions = [_region(rel_path, start_line, end_line) for _, start_line, end_line in functions]
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
                estimatedBenefit=_benefit(
                    {
                        "duplicationReduction": min(1.0, len(functions) / 3),
                        "maintainabilityGain": 0.42,
                    }
                ),
                estimatedRisk=_risk(
                    {
                        "semanticRisk": 0.28,
                        "apiRisk": 0.12,
                        "testRisk": 0.22,
                        "conflictRisk": 0.18,
                    }
                ),
                estimatedDiff=_diff(
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


def _build_remove_abstraction_candidates(
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
                anchorRegions=[_region(rel_path, start_line, end_line)],
                estimatedBenefit=_benefit(
                    {
                        "complexityReduction": min(1.0, length / max(LONG_FUNCTION_THRESHOLD, 1)),
                        "maintainabilityGain": 0.26,
                    }
                ),
                estimatedRisk=_risk(
                    {
                        "semanticRisk": 0.2,
                        "apiRisk": 0.08,
                        "testRisk": 0.18,
                        "conflictRisk": 0.12,
                    }
                ),
                estimatedDiff=_diff(
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


def _exported_names(tree: ast.AST) -> set[str]:
    exported: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets):
            continue
        if not isinstance(node.value, (ast.List, ast.Tuple)):
            continue
        for element in node.value.elts:
            if isinstance(element, ast.Constant) and isinstance(element.value, str):
                exported.add(element.value)
    return exported



def _referenced_names(tree: ast.AST) -> set[str]:
    return {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }



def _module_name(root: Path, path: Path) -> str:
    relative = path.relative_to(root)
    if relative.name == "__init__.py":
        parts = relative.with_suffix("").parts[:-1]
    else:
        parts = relative.with_suffix("").parts
    return ".".join(parts)


def _known_python_modules(root: Path) -> dict[str, str]:
    modules: dict[str, str] = {}
    for path in walk_source_files(root, (".py",)):
        module_name = _module_name(root, path)
        if module_name:
            modules[module_name] = path.relative_to(root).as_posix()
    return modules


def _resolve_import_targets(
    current_module: str,
    is_package: bool,
    node: ast.AST,
    known_modules: dict[str, str],
) -> set[str]:
    targets: set[str] = set()
    if isinstance(node, ast.Import):
        for alias in node.names:
            if alias.name in known_modules:
                targets.add(alias.name)
        return targets
    if not isinstance(node, ast.ImportFrom):
        return targets

    base_parts = current_module.split(".") if current_module else []
    if not is_package and base_parts:
        base_parts = base_parts[:-1]
    if node.level > 1:
        base_parts = base_parts[: -(node.level - 1)] if len(base_parts) >= node.level - 1 else []

    module_parts = node.module.split(".") if node.module else []
    candidate_parts = [*base_parts, *module_parts] if node.level else module_parts
    candidate_module = ".".join(part for part in candidate_parts if part)
    if candidate_module in known_modules:
        targets.add(candidate_module)
    for alias in node.names:
        if alias.name == "*":
            continue
        child_module = ".".join(part for part in [candidate_module, alias.name] if part)
        if child_module in known_modules:
            targets.add(child_module)
    return targets


def _strongly_connected_components(graph: dict[str, set[str]]) -> list[list[str]]:
    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    components: list[list[str]] = []

    def visit(node: str) -> None:
        nonlocal index
        indices[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)

        for neighbor in graph[node]:
            if neighbor not in indices:
                visit(neighbor)
                lowlinks[node] = min(lowlinks[node], lowlinks[neighbor])
            elif neighbor in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[neighbor])

        if lowlinks[node] != indices[node]:
            return

        component: list[str] = []
        while stack:
            member = stack.pop()
            on_stack.remove(member)
            component.append(member)
            if member == node:
                break
        components.append(sorted(component))

    for node in sorted(graph):
        if node not in indices:
            visit(node)
    return components


def _build_cycle_candidates(graph: dict[str, set[str]], module_to_file: dict[str, str]) -> list[Candidate]:
    components = [component for component in _strongly_connected_components(graph) if len(component) > 1]
    candidates: list[Candidate] = []
    for component in components:
        files = sorted(module_to_file[module] for module in component)
        cycle_id = "-".join(file.replace("/", "-").replace(".", "-") for file in files)
        candidates.append(
            Candidate(
                id=f"py-reduce-cycle-{cycle_id}",
                kind="reduce_cycle",
                title=f"Reduce import cycle across {len(files)} Python modules",
                description="Python module import cycle detected across " + ", ".join(f"`{file}`" for file in files),
                language="python",
                scope="package",
                source=["graph"],
                files=files,
                symbols=component,
                estimatedBenefit=_benefit({"cycleReduction": 1.0, "maintainabilityGain": 0.38}),
                estimatedRisk=_risk(
                    {
                        "semanticRisk": 0.42,
                        "apiRisk": 0.18,
                        "testRisk": 0.28,
                        "conflictRisk": 0.24,
                    }
                ),
                estimatedDiff=_diff(
                    {
                        "filesTouched": len(files),
                        "linesAdded": max(4, len(files) * 3),
                        "linesModified": max(2, len(files) * 4),
                    }
                ),
                confidence=0.74,
                applyModeHint="report_only",
                requiredChecks=["parse", "lint", "typecheck", "unit_test"],
                provenance=Provenance(
                    detectors=["python-import-graph-cycle"],
                    evidence=[*[f"module:{module}" for module in component], *[f"file:{file}" for file in files]],
                ),
            )
        )
    return candidates

class PythonAdapter:
    name: str = "python"
    extensions: tuple[str, ...] = (".py",)

    def supports(self, root: Path) -> bool:
        return any(True for _ in walk_source_files(root, self.extensions))

    def scan(self, root: Path) -> list[Candidate]:
        candidates: list[Candidate] = []
        module_to_file = _known_python_modules(root)
        graph: dict[str, set[str]] = defaultdict(set)
        for path in walk_source_files(root, self.extensions):
            file_candidates, imports = self._scan_file(root, path, module_to_file)
            candidates.extend(file_candidates)
            current_module = _module_name(root, path)
            if current_module:
                graph.setdefault(current_module, set()).update(imports)
        for module_name in module_to_file:
            graph.setdefault(module_name, set())
        candidates.extend(_build_cycle_candidates(graph, module_to_file))
        return candidates

    def _scan_file(
        self, root: Path, path: Path, known_modules: dict[str, str]
    ) -> tuple[list[Candidate], set[str]]:
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return [], set()

        rel_path = path.relative_to(root).as_posix()
        lines = source.splitlines()
        referenced_names = _referenced_names(tree)
        exported_names = _exported_names(tree)
        current_module = _module_name(root, path)
        is_package = path.name == "__init__.py"
        imports: set[str] = set()
        candidates: list[Candidate] = []
        duplicate_functions: list[tuple[str, int, int, str]] = []
        passthrough_functions: list[tuple[str, int, int, str]] = []
        top_level_statements = len(tree.body)
        if len(lines) >= LARGE_MODULE_THRESHOLD or top_level_statements >= TOP_LEVEL_STATEMENT_THRESHOLD:
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
                    estimatedBenefit=_benefit(
                        {
                            "complexityReduction": min(1.0, len(lines) / LARGE_MODULE_THRESHOLD),
                            "maintainabilityGain": 0.4,
                        }
                    ),
                    estimatedRisk=_risk(
                        {
                            "semanticRisk": 0.45,
                            "apiRisk": 0.25,
                            "testRisk": 0.3,
                            "conflictRisk": 0.2,
                        }
                    ),
                    estimatedDiff=_diff(
                        {
                            "filesTouched": 1,
                            "linesAdded": max(8, len(lines) // 5),
                            "linesModified": len(lines),
                        }
                    ),
                    confidence=0.7,
                    applyModeHint="report_only",
                    requiredChecks=["parse", "lint", "typecheck", "unit_test"],
                    provenance=Provenance(
                        detectors=["python-ast-large-module"],
                        evidence=[f"line_span:{len(lines)}", f"top_level_statements:{top_level_statements}"],
                    ),
                )
            )

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "__future__":
                continue
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    bound_name = alias.asname or alias.name.split(".")[0]
                    if bound_name in referenced_names:
                        continue
                    candidates.append(
                        Candidate(
                            id=f"py-unused-import-{rel_path}-{node.lineno}-{bound_name}",
                            kind="unused_import",
                            title=f"Remove unused import {bound_name}",
                            description=f"Unused Python import `{bound_name}` in {rel_path}",
                            language="python",
                            scope="local",
                            source=["static"],
                            files=[rel_path],
                            symbols=[bound_name],
                            anchorRegions=[_region(rel_path, node.lineno, node.end_lineno or node.lineno)],
                            estimatedBenefit=_benefit({"maintainabilityGain": 0.08}),
                            estimatedRisk=_risk({"semanticRisk": 0.02, "conflictRisk": 0.03}),
                            estimatedDiff=_diff(
                                {"filesTouched": 1, "linesDeleted": 1, "linesModified": 1}
                            ),
                            confidence=0.95,
                            applyModeHint="auto",
                            requiredChecks=["parse", "lint", "typecheck"],
                            provenance=Provenance(
                                detectors=["python-ast-unused-import"],
                                evidence=[f"line:{node.lineno}", f"symbol:{bound_name}"],
                            ),
                        )
                    )
                imports.update(_resolve_import_targets(current_module, is_package, node, known_modules))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.end_lineno is not None:
                length = node.end_lineno - node.lineno + 1
                duplicate_functions.append((node.name, node.lineno, node.end_lineno, _duplicate_function_key(node)))
                passthrough_target = _passthrough_target(node)
                if passthrough_target and node.name.startswith("_") and not node.name.startswith("__") and node.name not in exported_names:
                    passthrough_functions.append((node.name, node.lineno, node.end_lineno, passthrough_target))
                if length < LONG_FUNCTION_THRESHOLD:
                    continue
                candidates.append(
                    Candidate(
                        id=f"py-extract-function-{rel_path}-{node.lineno}-{node.name}",
                        kind="extract_function",
                        title=f"Extract logic from long function {node.name}",
                        description=(
                            f"Function `{node.name}` in {rel_path} spans {length} lines and is a"
                            " candidate for extraction"
                        ),
                        language="python",
                        scope="local",
                        source=["static", "metric"],
                        files=[rel_path],
                        symbols=[node.name],
                        anchorRegions=[_region(rel_path, node.lineno, node.end_lineno)],
                        estimatedBenefit=_benefit(
                            {
                                "complexityReduction": min(1.0, length / max(len(lines), 1)),
                                "maintainabilityGain": 0.35,
                            }
                        ),
                        estimatedRisk=_risk(
                            {
                                "semanticRisk": 0.35,
                                "testRisk": 0.25,
                                "conflictRisk": 0.15,
                            }
                        ),
                        estimatedDiff=_diff(
                            {
                                "filesTouched": 1,
                                "linesAdded": max(3, length // 4),
                                "linesModified": length,
                            }
                        ),
                        confidence=0.72,
                        applyModeHint="guarded",
                        requiredChecks=["parse", "lint", "typecheck", "unit_test"],
                        provenance=Provenance(
                            detectors=["python-ast-long-function"],
                            evidence=[f"line_span:{length}", f"symbol:{node.name}"],
                        ),
                    )
                )

        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.end_lineno is None:
                continue
            if not node.name.startswith("_") or node.name.startswith("__"):
                continue
            if node.name in referenced_names or node.name in exported_names:
                continue
            length = node.end_lineno - node.lineno + 1
            candidates.append(
                Candidate(
                    id=f"py-dead-code-{rel_path}-{node.lineno}-{node.name}",
                    kind="dead_code",
                    title=f"Remove unused private function {node.name}",
                    description=f"Top-level private function `{node.name}` in {rel_path} is not referenced",
                    language="python",
                    scope="module",
                    source=["static"],
                    files=[rel_path],
                    symbols=[node.name],
                    anchorRegions=[_region(rel_path, node.lineno, node.end_lineno)],
                    estimatedBenefit=_benefit({"maintainabilityGain": 0.18}),
                    estimatedRisk=_risk({"semanticRisk": 0.08, "conflictRisk": 0.04}),
                    estimatedDiff=_diff(
                        {
                            "filesTouched": 1,
                            "linesDeleted": length,
                            "linesModified": length,
                        }
                    ),
                    confidence=0.86,
                    applyModeHint="auto",
                    requiredChecks=["parse", "lint", "typecheck"],
                    provenance=Provenance(
                        detectors=["python-ast-dead-code"],
                        evidence=[f"line_span:{length}", f"symbol:{node.name}"],
                    ),
                )
            )

        candidates.extend(_build_duplicate_candidates(rel_path, duplicate_functions))
        candidates.extend(_build_remove_abstraction_candidates(rel_path, passthrough_functions))
        return candidates, imports
