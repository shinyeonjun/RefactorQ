from __future__ import annotations

from collections import Counter
from typing import Literal
import ast

from refactorq.core.candidate.models import (
    AnchorRegion,
    BoundaryImpact,
    EstimatedBenefit,
    EstimatedDiff,
    EstimatedRisk,
)


LONG_FUNCTION_THRESHOLD = 35
LARGE_MODULE_THRESHOLD = 300
TOP_LEVEL_STATEMENT_THRESHOLD = 18
DUPLICATE_FUNCTION_MIN_LINES = 3
INLINE_FUNCTION_MAX_LINES = 8
CLIENT_LAYER_TOKENS = {"frontend", "client", "web", "ui"}
SERVER_LAYER_TOKENS = {"backend", "server", "api", "controller", "controllers"}


def region(file: str, start_line: int, end_line: int) -> AnchorRegion:
    return AnchorRegion.model_validate({"file": file, "startLine": start_line, "endLine": end_line})


def benefit(payload: dict[str, float]) -> EstimatedBenefit:
    return EstimatedBenefit.model_validate(payload)


def risk(payload: dict[str, float]) -> EstimatedRisk:
    return EstimatedRisk.model_validate(payload)


def diff(payload: dict[str, int]) -> EstimatedDiff:
    return EstimatedDiff.model_validate(payload)


def layer_boundary_impact(producer_side: str, consumer_side: str) -> BoundaryImpact:
    return BoundaryImpact(
        crossLanguage=False,
        boundaryTypes=[],
        producerSide=[producer_side],
        consumerSide=[consumer_side],
        contractArtifacts=[],
        impactLevel="medium",
    )


def duplicate_function_key(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    args_dump = ast.dump(node.args, annotate_fields=False, include_attributes=False)
    body_dump = ast.dump(ast.Module(body=node.body, type_ignores=[]), annotate_fields=False, include_attributes=False)
    return f"{args_dump}|{body_dump}"


def passthrough_target(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
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


def is_private_unexported_name(name: str, exported_names: set[str]) -> bool:
    return name.startswith("_") and not name.startswith("__") and name not in exported_names


def is_side_effect_free_python_initializer(node: ast.AST | None) -> bool:
    if node is None:
        return False
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub, ast.Not, ast.Invert)):
        return is_side_effect_free_python_initializer(node.operand)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return all(is_side_effect_free_python_initializer(element) for element in node.elts)
    if isinstance(node, ast.Dict):
        return all(
            (key is None or is_side_effect_free_python_initializer(key))
            and is_side_effect_free_python_initializer(value)
            for key, value in zip(node.keys, node.values, strict=False)
        )
    return False


def unused_import_apply_mode(raw_lines: list[str], node: ast.Import | ast.ImportFrom, bound_name: str) -> Literal["auto", "report_only"]:
    if node.lineno < 1 or node.lineno > len(raw_lines):
        return "report_only"
    if node.end_lineno != node.lineno:
        return "report_only"
    line = raw_lines[node.lineno - 1]
    stripped = line.strip()
    if not stripped or not stripped.startswith(("import ", "from ")) or "\\" in stripped:
        return "report_only"
    if "#" in line or "(" in line or ")" in line:
        return "report_only"
    if isinstance(node, ast.Import):
        specifiers = [part.strip() for part in stripped[len("import ") :].split(",")]
    else:
        if " import " not in stripped:
            return "report_only"
        specifiers = [part.strip() for part in stripped.split(" import ", 1)[1].split(",")]
    return "auto" if any((" as " in specifier and specifier.rsplit(" as ", 1)[1].strip() == bound_name) or specifier.split(".", 1)[0].strip() == bound_name for specifier in specifiers) else "report_only"


def exported_names(tree: ast.AST) -> set[str]:
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


def loaded_name_counts(tree: ast.AST) -> Counter[str]:
    counts: Counter[str] = Counter()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            counts[node.id] += 1
    return counts
