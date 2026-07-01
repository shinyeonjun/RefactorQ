from __future__ import annotations

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


def _region(file: str, start_line: int, end_line: int) -> AnchorRegion:
    return AnchorRegion.model_validate({"file": file, "startLine": start_line, "endLine": end_line})


def _benefit(payload: dict[str, float]) -> EstimatedBenefit:
    return EstimatedBenefit.model_validate(payload)


def _risk(payload: dict[str, float]) -> EstimatedRisk:
    return EstimatedRisk.model_validate(payload)


def _diff(payload: dict[str, int]) -> EstimatedDiff:
    return EstimatedDiff.model_validate(payload)


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

class PythonAdapter:
    name: str = "python"
    extensions: tuple[str, ...] = (".py",)

    def supports(self, root: Path) -> bool:
        return any(True for _ in walk_source_files(root, self.extensions))

    def scan(self, root: Path) -> list[Candidate]:
        candidates: list[Candidate] = []
        for path in walk_source_files(root, self.extensions):
            candidates.extend(self._scan_file(root, path))
        return candidates

    def _scan_file(self, root: Path, path: Path) -> list[Candidate]:
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return []

        rel_path = path.relative_to(root).as_posix()
        lines = source.splitlines()
        referenced_names = _referenced_names(tree)
        exported_names = _exported_names(tree)
        candidates: list[Candidate] = []



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
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.end_lineno is not None:
                length = node.end_lineno - node.lineno + 1
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
        return candidates
