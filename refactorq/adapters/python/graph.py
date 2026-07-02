from __future__ import annotations

from pathlib import Path
import ast

from refactorq.adapters.python.common import CLIENT_LAYER_TOKENS, SERVER_LAYER_TOKENS, benefit, diff, risk
from refactorq.core.candidate.models import Candidate, Provenance
from refactorq.core.filesystem import walk_source_files


def module_name(root: Path, path: Path) -> str:
    relative = path.relative_to(root)
    if relative.name == "__init__.py":
        parts = relative.with_suffix("").parts[:-1]
    else:
        parts = relative.with_suffix("").parts
    return ".".join(parts)


def path_layer(rel_path: str) -> str | None:
    tokens = {part.lower() for part in Path(rel_path).parts}
    if tokens & CLIENT_LAYER_TOKENS:
        return "client"
    if tokens & SERVER_LAYER_TOKENS:
        return "server"
    return None


def known_python_modules(root: Path) -> dict[str, str]:
    modules: dict[str, str] = {}
    for path in walk_source_files(root, (".py",)):
        name = module_name(root, path)
        if name:
            modules[name] = path.relative_to(root).as_posix()
    return modules


def resolve_import_targets(
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


def strongly_connected_components(graph: dict[str, set[str]]) -> list[list[str]]:
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


def build_cycle_candidates(graph: dict[str, set[str]], module_to_file: dict[str, str]) -> list[Candidate]:
    components = [component for component in strongly_connected_components(graph) if len(component) > 1]
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
                estimatedBenefit=benefit({"cycleReduction": 1.0, "maintainabilityGain": 0.38}),
                estimatedRisk=risk(
                    {
                        "semanticRisk": 0.42,
                        "apiRisk": 0.18,
                        "testRisk": 0.28,
                        "conflictRisk": 0.24,
                    }
                ),
                estimatedDiff=diff(
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
