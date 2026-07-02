from __future__ import annotations

import json

from collections.abc import Iterator
from contextlib import contextmanager
import subprocess
import sys
from typing import cast
from pathlib import Path
from types import SimpleNamespace

from pytest import MonkeyPatch
from typer.testing import CliRunner

from refactorq.agents.codex import BoundedPatchScope, CodexGuardedApplier, GuardedApplyRequest, GuardedApplyResult, GuardedRepairRequest

from refactorq.cli.main import app
from refactorq.core.candidate import Candidate
from refactorq.core.execution.guarded import repair_guarded_changes as _repair_guarded_changes
from refactorq.core.execution.report import report_plan
from refactorq.core.execution.run import run_plan
from refactorq.core.execution.service import _apply_plan_internal
from refactorq.core.planning import PlanResult, ProposalRevalidation, SolverProposal

from refactorq.core.repo.models import RepoManifestMap, RepoSnapshot
from refactorq.core.service import RefactorQService
from refactorq.core.verification import VerificationCheckResult, VerificationResult
from refactorq.core.verification.command_checks import npm_command as _npm_command
from refactorq.core.verification.service import verify_repo

runner = CliRunner()



def _long_python_function() -> str:
    body = ["def very_long_function():"]
    body.extend(f"    value_{index} = {index}" for index in range(40))
    body.append("    return value_39")
    body.append("")
    return "\n".join(body)


def _openapi_contract() -> str:
    return "openapi: 3.1.0\npaths:\n  /items:\n    get:\n      operationId: listItems\n"



def _wide_inline_python_module() -> str:
    body = [
        "def _normalize_value(value):",
        "    cleaned = value.strip()",
        "    return cleaned.lower()",
        "",
        "def format_value(value):",
        "    return _normalize_value(value)",
        "",
    ]
    body.extend(f"def filler_{index}():\n    return {index}\n" for index in range(12))
    body.append('print(format_value("ok"))')
    body.append("")
    return "\n".join(body)



def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )


def test_codex_apply_request_preserves_selected_candidate_scope() -> None:
    candidate = Candidate.model_validate(
        {
            "id": "py-extract-function-sample.py-1-very_long_function",
            "kind": "extract_function",
            "title": "Extract helper",
            "description": "Synthetic guarded extract candidate",
            "language": "python",
            "scope": "local",
            "source": ["static"],
            "files": ["sample.py"],
            "symbols": ["very_long_function"],
            "anchorRegions": [{"file": "sample.py", "startLine": 1, "endLine": 20}],
            "applyModeHint": "guarded",
            "requiredChecks": ["parse", "lint", "typecheck", "unit_test"],
        }
    )

    request = CodexGuardedApplier().build_apply_request(candidate)

    assert isinstance(request, GuardedApplyRequest)
    assert request.mode == "apply"
    assert request.scope == BoundedPatchScope(
        candidateIds=[candidate.id],
        allowedFiles=["sample.py"],
        anchorRegions=candidate.anchor_regions,
        requiredChecks=["parse", "lint", "typecheck", "unit_test"],
    )
    assert request.candidate.id == candidate.id


def test_codex_repair_request_preserves_selected_candidate_scope() -> None:
    first = Candidate.model_validate(
        {
            "id": "py-inline-function-sample.py-1-_normalize_value",
            "kind": "inline_function",
            "title": "Inline helper",
            "description": "Synthetic guarded inline candidate",
            "language": "python",
            "scope": "module",
            "source": ["static"],
            "files": ["sample.py"],
            "symbols": ["_normalize_value"],
            "anchorRegions": [{"file": "sample.py", "startLine": 1, "endLine": 3}],
            "applyModeHint": "guarded",
            "requiredChecks": ["parse", "lint", "typecheck"],
        }
    )
    second = Candidate.model_validate(
        {
            "id": "py-remove-abstraction-sample.py-10-_wrapper",
            "kind": "remove_abstraction",
            "title": "Remove wrapper",
            "description": "Synthetic guarded wrapper candidate",
            "language": "python",
            "scope": "module",
            "source": ["static"],
            "files": ["sample.py"],
            "symbols": ["_wrapper"],
            "anchorRegions": [{"file": "sample.py", "startLine": 10, "endLine": 12}],
            "applyModeHint": "guarded",
            "requiredChecks": ["parse", "lint", "unit_test"],
        }
    )
    verification = VerificationResult(status="failed", checks=[])

    request = CodexGuardedApplier().build_repair_request([first, second], verification)

    assert isinstance(request, GuardedRepairRequest)
    assert request.mode == "repair"
    assert request.scope.candidate_ids == [first.id, second.id]
    assert request.scope.allowed_files == ["sample.py"]
    assert request.scope.anchor_regions == [*first.anchor_regions, *second.anchor_regions]
    assert request.scope.required_checks == ["lint", "parse", "typecheck", "unit_test"]
    assert request.verification.status == "failed"


def test_apply_removes_python_unused_import(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text("import os\n\nprint('hi')\n", encoding="utf-8")

    result = RefactorQService().apply(tmp_path, "safe")

    assert result.status == "applied"
    assert result.changed_files == ["sample.py"]
    assert [candidate.id for candidate in result.applied_candidates] == ["py-unused-import-sample.py-1-os"]
    assert sample.read_text(encoding="utf-8") == "\nprint('hi')\n"


def test_apply_removes_python_private_dead_code(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text("def _helper():\n    return 1\n\nprint('hi')\n", encoding="utf-8")

    result = RefactorQService().apply(tmp_path, "safe")

    assert result.status == "applied"
    assert result.changed_files == ["sample.py"]
    assert [candidate.kind for candidate in result.applied_candidates] == ["dead_code"]
    assert sample.read_text(encoding="utf-8") == "\nprint('hi')\n"


def test_apply_removes_typescript_unused_symbol(tmp_path: Path) -> None:
    sample = tmp_path / "sample.ts"
    sample.write_text("function helper() {\n  return 1;\n}\n\nconsole.log('ok');\n", encoding="utf-8")

    result = RefactorQService().apply(tmp_path, "safe")

    assert result.status == "applied"
    assert result.changed_files == ["sample.ts"]
    assert [candidate.kind for candidate in result.applied_candidates] == ["unused_symbol"]
    assert sample.read_text(encoding="utf-8") == "\nconsole.log('ok');\n"



def test_apply_rewrites_named_typescript_unused_imports(tmp_path: Path) -> None:
    sample = tmp_path / "sample.ts"
    sample.write_text('import { readFile, writeFile } from "node:fs";\n\nconsole.log(writeFile);\n', encoding="utf-8")

    result = RefactorQService().apply(tmp_path, "safe")

    assert result.status == "applied"
    assert result.changed_files == ["sample.ts"]
    assert sample.read_text(encoding="utf-8") == 'import { writeFile } from "node:fs";\n\nconsole.log(writeFile);\n'


def test_apply_removes_type_only_typescript_unused_import(tmp_path: Path) -> None:
    sample = tmp_path / "sample.ts"
    sample.write_text('import type { Foo } from "./types";\n\nconsole.log("ok");\n', encoding="utf-8")

    result = RefactorQService().apply(tmp_path, "safe")

    assert result.status == "applied"
    assert result.changed_files == ["sample.ts"]
    assert [candidate.kind for candidate in result.applied_candidates] == ["unused_import"]
    assert sample.read_text(encoding="utf-8") == '\nconsole.log("ok");\n'



def test_balanced_apply_uses_guarded_codex_flow(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(_long_python_function(), encoding="utf-8")

    monkeypatch.setattr("refactorq.core.execution.service.CodexGuardedApplier.is_available", lambda self: True)

    def fake_apply(self: CodexGuardedApplier, root: Path, candidate: Candidate) -> GuardedApplyResult:
        target = root / candidate.files[0]
        target.write_text(
            "def _very_long_function_impl():\n    return 39\n\n\ndef very_long_function():\n    return _very_long_function_impl()\n",
            encoding="utf-8",
        )
        return GuardedApplyResult(
            status="applied",
            candidateIds=[candidate.id],
            touchedFiles=[candidate.files[0]],
            summary=["extracted helper"],
            details={"candidate": candidate.id},
        )

    monkeypatch.setattr("refactorq.core.execution.service.CodexGuardedApplier.apply", fake_apply)

    result = RefactorQService().apply(tmp_path, "balanced")

    assert result.status == "applied"
    assert result.changed_files == ["sample.py"]
    assert [candidate.kind for candidate in result.applied_candidates] == ["extract_function"]
    assert "_very_long_function_impl" in sample.read_text(encoding="utf-8")


def test_balanced_run_executes_low_impact_cross_language_guarded_candidate(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    backend = tmp_path / "backend"
    frontend = tmp_path / "frontend"
    backend.mkdir()
    frontend.mkdir()
    (tmp_path / "openapi.yaml").write_text(_openapi_contract(), encoding="utf-8")
    sample = backend / "api.py"
    sample.write_text('ROUTE = "/items"\n\n' + _long_python_function(), encoding="utf-8")
    (frontend / "client.ts").write_text('const endpoint = "/items";\nconsole.log(endpoint);\n', encoding="utf-8")

    monkeypatch.setattr("refactorq.core.execution.service.CodexGuardedApplier.is_available", lambda self: True)

    def fake_apply(self: CodexGuardedApplier, root: Path, candidate: Candidate) -> GuardedApplyResult:
        target = root / candidate.files[0]
        target.write_text(
            'ROUTE = "/items"\n\n'
            "def _very_long_function_impl():\n    return 39\n\n\n"
            "def very_long_function():\n    return _very_long_function_impl()\n",
            encoding="utf-8",
        )
        return GuardedApplyResult(
            status="applied",
            candidateIds=[candidate.id],
            touchedFiles=[candidate.files[0]],
            summary=["extracted helper in boundary-adjacent file"],
            details={"candidate": candidate.id},
        )

    monkeypatch.setattr("refactorq.core.execution.service.CodexGuardedApplier.apply", fake_apply)

    def fake_verify_repo(root: Path, *, required_checks: list[str] | None = None, candidates: list[Candidate] | None = None) -> VerificationResult:
        return VerificationResult(
            status="passed",
            checks=[
                VerificationCheckResult(name="boundary_contracts", kind="build", status="passed", evidence=["contract ok"], details={}),
                VerificationCheckResult(name="boundary_integration", kind="integration_test", status="passed", evidence=["integration ok"], details={}),
            ],
        )

    monkeypatch.setattr("refactorq.core.execution.service.verify_repo", fake_verify_repo)


    result = RefactorQService().run(tmp_path, "balanced")

    assert result.status == "passed"
    assert [candidate.kind for candidate in result.apply.applied_candidates] == ["extract_function"]
    assert any(check.name == "boundary_contracts" and check.status == "passed" for check in result.verification.checks)
    assert any(check.name == "boundary_integration" and check.status == "passed" for check in result.verification.checks)

    assert "_very_long_function_impl" in sample.read_text(encoding="utf-8")


def test_balanced_run_executes_guarded_typescript_inline_function_candidate(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    sample = tmp_path / "sample.ts"
    sample.write_text(
        "const _normalizeValue = (value: string) => {\n"
        "  const cleaned = value.trim();\n"
        "  return cleaned.toLowerCase();\n"
        "};\n\n"
        "const formatValue = (value: string) => _normalizeValue(value);\n\n"
        "console.log(formatValue(\"ok\"));\n",
        encoding="utf-8",
    )

    monkeypatch.setattr("refactorq.core.execution.service.CodexGuardedApplier.is_available", lambda self: True)

    def fake_apply(self: CodexGuardedApplier, root: Path, candidate: Candidate) -> GuardedApplyResult:
        target = root / candidate.files[0]
        target.write_text(
            "const formatValue = (value: string) => {\n"
            "  const cleaned = value.trim();\n"
            "  return cleaned.toLowerCase();\n"
            "};\n\n"
            "console.log(formatValue(\"ok\"));\n",
            encoding="utf-8",
        )
        return GuardedApplyResult(
            status="applied",
            candidateIds=[candidate.id],
            touchedFiles=[candidate.files[0]],
            summary=["inlined TypeScript helper"],
            details={"candidate": candidate.id},
        )

    monkeypatch.setattr("refactorq.core.execution.service.CodexGuardedApplier.apply", fake_apply)

    def fake_verify_repo(root: Path, *, required_checks: list[str] | None = None, candidates: list[Candidate] | None = None) -> VerificationResult:
        return VerificationResult(
            status="passed",
            checks=[
                VerificationCheckResult(name="typescript_typecheck", kind="typecheck", status="passed", evidence=["typescript ok"], details={}),
            ],
        )

    monkeypatch.setattr("refactorq.core.execution.service.verify_repo", fake_verify_repo)


    result = RefactorQService().run(tmp_path, "balanced")

    assert result.status == "passed"
    assert [candidate.kind for candidate in result.apply.applied_candidates] == ["inline_function"]
    assert any(check.name == "typescript_typecheck" and check.status == "passed" for check in result.verification.checks)
    assert "_normalizeValue" not in sample.read_text(encoding="utf-8")


def test_balanced_apply_executes_low_impact_cross_language_auto_candidate(tmp_path: Path) -> None:
    backend = tmp_path / "backend"
    frontend = tmp_path / "frontend"
    backend.mkdir()
    frontend.mkdir()
    (tmp_path / "openapi.yaml").write_text(_openapi_contract(), encoding="utf-8")
    sample = backend / "api.py"
    sample.write_text('import os\n\nROUTE = "/items"\nprint(ROUTE)\n', encoding="utf-8")
    (frontend / "client.ts").write_text('const endpoint = "/items";\nconsole.log(endpoint);\n', encoding="utf-8")

    result = RefactorQService().run(tmp_path, "balanced")

    assert result.status == "passed"
    assert [candidate.id for candidate in result.apply.applied_candidates] == ["py-unused-import-backend/api.py-1-os"]
    assert any(check.name == "boundary_contracts" and check.status == "passed" for check in result.verification.checks)
    assert sample.read_text(encoding="utf-8") == '\nROUTE = "/items"\nprint(ROUTE)\n'

def test_balanced_run_rolls_back_when_boundary_consumer_surface_is_missing(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    backend = tmp_path / "backend"
    shared = tmp_path / "shared"
    backend.mkdir()
    shared.mkdir()
    (tmp_path / "openapi.yaml").write_text(_openapi_contract(), encoding="utf-8")
    sample = backend / "api.py"
    sample.write_text('ROUTE = "/items"\n\n' + _long_python_function(), encoding="utf-8")
    (shared / "common.ts").write_text("console.log('ok');\n", encoding="utf-8")

    monkeypatch.setattr("refactorq.core.execution.service.CodexGuardedApplier.is_available", lambda self: True)

    def fake_apply(self: CodexGuardedApplier, root: Path, candidate: Candidate) -> GuardedApplyResult:
        target = root / candidate.files[0]
        target.write_text(
            "def _very_long_function_impl():\n    return 39\n\n\n"
            "def very_long_function():\n    return _very_long_function_impl()\n",
            encoding="utf-8",
        )
        return GuardedApplyResult(
            status="applied",
            candidateIds=[candidate.id],
            touchedFiles=[candidate.files[0]],
            summary=["extracted helper in boundary-adjacent file"],
            details={"candidate": candidate.id},
        )

    monkeypatch.setattr("refactorq.core.execution.service.CodexGuardedApplier.apply", fake_apply)

    result = RefactorQService().run(tmp_path, "balanced")

    assert result.status == "rejected_no_batch"
    assert result.apply.status == "rejected_no_batch"
    assert result.apply.applied_candidates == []
    assert sample.read_text(encoding="utf-8") == 'ROUTE = "/items"\n\n' + _long_python_function()



def test_balanced_apply_uses_guarded_remove_abstraction_flow(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(
        "def normalize(value):\n    return value.strip().lower()\n\n"
        "def _normalize_wrapper(value):\n    return normalize(value)\n\n"
        "print(_normalize_wrapper(\"ok\"))\n",
        encoding="utf-8",
    )

    monkeypatch.setattr("refactorq.core.execution.service.CodexGuardedApplier.is_available", lambda self: True)

    def fake_apply(self: CodexGuardedApplier, root: Path, candidate: Candidate) -> GuardedApplyResult:
        target = root / candidate.files[0]
        target.write_text(
            "def normalize(value):\n    return value.strip().lower()\n\n"
            "def format_value(value):\n    return normalize(value)\n",
            encoding="utf-8",
        )
        return GuardedApplyResult(
            status="applied",
            candidateIds=[candidate.id],
            touchedFiles=[candidate.files[0]],
            summary=["removed thin wrapper abstraction"],
            details={"candidate": candidate.id},
        )

    monkeypatch.setattr("refactorq.core.execution.service.CodexGuardedApplier.apply", fake_apply)

    result = RefactorQService().apply(tmp_path, "balanced")

    assert result.status == "applied"
    assert result.changed_files == ["sample.py"]
    assert "remove_abstraction" in [candidate.kind for candidate in result.applied_candidates]
    assert "_normalize_wrapper" not in sample.read_text(encoding="utf-8")


def test_balanced_apply_uses_guarded_inline_function_flow(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(
        "def _normalize_value(value):\n    cleaned = value.strip()\n    return cleaned.lower()\n\n"
        "def format_value(value):\n    return _normalize_value(value)\n\n"
        "print(format_value(\"ok\"))\n",
        encoding="utf-8",
    )

    monkeypatch.setattr("refactorq.core.execution.service.CodexGuardedApplier.is_available", lambda self: True)

    def fake_apply(self: CodexGuardedApplier, root: Path, candidate: Candidate) -> GuardedApplyResult:
        target = root / candidate.files[0]
        target.write_text(
            "def format_value(value):\n    cleaned = value.strip()\n    return cleaned.lower()\n\n"
            "print(format_value(\"ok\"))\n",
            encoding="utf-8",
        )
        return GuardedApplyResult(
            status="applied",
            candidateIds=[candidate.id],
            touchedFiles=[candidate.files[0]],
            summary=["inlined single-use helper"],
            details={"candidate": candidate.id},
        )

    monkeypatch.setattr("refactorq.core.execution.service.CodexGuardedApplier.apply", fake_apply)

    result = RefactorQService().apply(tmp_path, "balanced")

    assert result.status == "applied"
    assert result.changed_files == ["sample.py"]
    assert "inline_function" in [candidate.kind for candidate in result.applied_candidates]
    assert "_normalize_value" not in sample.read_text(encoding="utf-8")


def test_balanced_apply_uses_guarded_typescript_inline_function_flow(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    sample = tmp_path / "sample.ts"
    sample.write_text(
        "const _normalizeValue = (value: string) => {\n"
        "  const cleaned = value.trim();\n"
        "  return cleaned.toLowerCase();\n"
        "};\n\n"
        "const formatValue = (value: string) => _normalizeValue(value);\n\n"
        "console.log(formatValue(\"ok\"));\n",
        encoding="utf-8",
    )

    monkeypatch.setattr("refactorq.core.execution.service.CodexGuardedApplier.is_available", lambda self: True)

    def fake_apply(self: CodexGuardedApplier, root: Path, candidate: Candidate) -> GuardedApplyResult:
        target = root / candidate.files[0]
        target.write_text(
            "const formatValue = (value: string) => {\n"
            "  const cleaned = value.trim();\n"
            "  return cleaned.toLowerCase();\n"
            "};\n\n"
            "console.log(formatValue(\"ok\"));\n",
            encoding="utf-8",
        )
        return GuardedApplyResult(
            status="applied",
            candidateIds=[candidate.id],
            touchedFiles=[candidate.files[0]],
            summary=["inlined TypeScript helper"],
            details={"candidate": candidate.id},
        )

    monkeypatch.setattr("refactorq.core.execution.service.CodexGuardedApplier.apply", fake_apply)

    result = RefactorQService().apply(tmp_path, "balanced")

    assert result.status == "applied"
    assert result.changed_files == ["sample.ts"]
    assert "inline_function" in [candidate.kind for candidate in result.applied_candidates]
    assert "_normalizeValue" not in sample.read_text(encoding="utf-8")


def test_balanced_apply_uses_guarded_typescript_extract_function_flow(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    sample = tmp_path / "sample.ts"
    body = "\n".join([f"  const value{index} = input + {index};" for index in range(38)])
    sample.write_text(
        "const formatValue = (input: number) => {\n"
        f"{body}\n"
        "  return input;\n"
        "};\n",
        encoding="utf-8",
    )

    monkeypatch.setattr("refactorq.core.execution.service.CodexGuardedApplier.is_available", lambda self: True)

    def fake_apply(self: CodexGuardedApplier, root: Path, candidate: Candidate) -> GuardedApplyResult:
        target = root / candidate.files[0]
        target.write_text(
            "const _formatValueImpl = (input: number) => {\n"
            "  return input;\n"
            "};\n\n"
            "const formatValue = (input: number) => _formatValueImpl(input);\n",
            encoding="utf-8",
        )
        return GuardedApplyResult(
            status="applied",
            candidateIds=[candidate.id],
            touchedFiles=[candidate.files[0]],
            summary=["extracted TypeScript helper"],
            details={"candidate": candidate.id},
        )

    monkeypatch.setattr("refactorq.core.execution.service.CodexGuardedApplier.apply", fake_apply)

    result = RefactorQService().apply(tmp_path, "balanced")

    assert result.status == "applied"
    assert result.changed_files == ["sample.ts"]
    assert [candidate.kind for candidate in result.applied_candidates] == ["extract_function"]
    assert "_formatValueImpl" in sample.read_text(encoding="utf-8")


def test_balanced_apply_uses_guarded_typescript_remove_abstraction_flow(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    sample = tmp_path / "sample.ts"
    sample.write_text(
        "const normalize = (value: string) => value.trim().toLowerCase();\n\n"
        "const _normalizeWrapper = (value: string) => normalize(value);\n\n"
        "console.log(_normalizeWrapper(\"ok\"));\n",
        encoding="utf-8",
    )

    monkeypatch.setattr("refactorq.core.execution.service.CodexGuardedApplier.is_available", lambda self: True)

    def fake_apply(self: CodexGuardedApplier, root: Path, candidate: Candidate) -> GuardedApplyResult:
        target = root / candidate.files[0]
        target.write_text(
            "const normalize = (value: string) => value.trim().toLowerCase();\n\n"
            "const formatValue = (value: string) => normalize(value);\n\n"
            "console.log(formatValue(\"ok\"));\n",
            encoding="utf-8",
        )
        return GuardedApplyResult(
            status="applied",
            candidateIds=[candidate.id],
            touchedFiles=[candidate.files[0]],
            summary=["removed thin TypeScript wrapper"],
            details={"candidate": candidate.id},
        )

    monkeypatch.setattr("refactorq.core.execution.service.CodexGuardedApplier.apply", fake_apply)

    result = RefactorQService().apply(tmp_path, "balanced")

    assert result.status == "applied"
    assert result.changed_files == ["sample.ts"]
    assert "remove_abstraction" in [candidate.kind for candidate in result.applied_candidates]
    assert "_normalizeWrapper" not in sample.read_text(encoding="utf-8")


def test_balanced_apply_uses_guarded_typescript_duplicate_logic_flow(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    sample = tmp_path / "sample.ts"
    sample.write_text(
        "const first = (value: string) => {\n"
        "  const cleaned = value.trim();\n"
        "  return cleaned.toLowerCase();\n"
        "};\n\n"
        "const second = (value: string) => {\n"
        "  const cleaned = value.trim();\n"
        "  return cleaned.toLowerCase();\n"
        "};\n",
        encoding="utf-8",
    )

    monkeypatch.setattr("refactorq.core.execution.service.CodexGuardedApplier.is_available", lambda self: True)

    def fake_apply(self: CodexGuardedApplier, root: Path, candidate: Candidate) -> GuardedApplyResult:
        target = root / candidate.files[0]
        target.write_text(
            "const normalizeValue = (value: string) => {\n"
            "  const cleaned = value.trim();\n"
            "  return cleaned.toLowerCase();\n"
            "};\n\n"
            "const first = (value: string) => normalizeValue(value);\n\n"
            "const second = (value: string) => normalizeValue(value);\n",
            encoding="utf-8",
        )
        return GuardedApplyResult(
            status="applied",
            candidateIds=[candidate.id],
            touchedFiles=[candidate.files[0]],
            summary=["consolidated duplicate TypeScript logic"],
            details={"candidate": candidate.id},
        )

    monkeypatch.setattr("refactorq.core.execution.service.CodexGuardedApplier.apply", fake_apply)

    result = RefactorQService().apply(tmp_path, "balanced")

    assert result.status == "applied"
    assert result.changed_files == ["sample.ts"]
    assert [candidate.kind for candidate in result.applied_candidates] == ["duplicate_logic"]
    assert "normalizeValue" in sample.read_text(encoding="utf-8")

def test_balanced_apply_uses_guarded_duplicate_logic_flow(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(
        "def first(value):\n    normalized = value.strip()\n    return normalized.lower()\n\n"
        "def second(value):\n    normalized = value.strip()\n    return normalized.lower()\n",
        encoding="utf-8",
    )

    monkeypatch.setattr("refactorq.core.execution.service.CodexGuardedApplier.is_available", lambda self: True)

    def fake_apply(self: CodexGuardedApplier, root: Path, candidate: Candidate) -> GuardedApplyResult:
        target = root / candidate.files[0]
        target.write_text(
            "def _normalize_value(value):\n    normalized = value.strip()\n    return normalized.lower()\n\n"
            "def first(value):\n    return _normalize_value(value)\n\n"
            "def second(value):\n    return _normalize_value(value)\n",
            encoding="utf-8",
        )
        return GuardedApplyResult(
            status="applied",
            candidateIds=[candidate.id],
            touchedFiles=[candidate.files[0]],
            summary=["consolidated duplicate logic"],
            details={"candidate": candidate.id},
        )

    monkeypatch.setattr("refactorq.core.execution.service.CodexGuardedApplier.apply", fake_apply)

    result = RefactorQService().apply(tmp_path, "balanced")

    assert result.status == "applied"
    assert result.changed_files == ["sample.py"]
    assert [candidate.kind for candidate in result.applied_candidates] == ["duplicate_logic"]
    assert "_normalize_value" in sample.read_text(encoding="utf-8")



def test_balanced_apply_rejects_guarded_scope_expansion(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    sample = tmp_path / "sample.py"
    helper = tmp_path / "helper.py"
    original = _long_python_function()
    sample.write_text(original, encoding="utf-8")
    helper.write_text("print('helper')\n", encoding="utf-8")

    monkeypatch.setattr("refactorq.core.execution.service.CodexGuardedApplier.is_available", lambda self: True)

    def fake_apply(self: CodexGuardedApplier, root: Path, candidate: Candidate) -> GuardedApplyResult:
        (root / candidate.files[0]).write_text("def very_long_function():\n    return 39\n", encoding="utf-8")
        (root / "helper.py").write_text("print('changed')\n", encoding="utf-8")
        return GuardedApplyResult(
            status="applied",
            candidateIds=[candidate.id],
            touchedFiles=[candidate.files[0], "helper.py"],
            summary=["touched helper too"],
            details={},
        )

    monkeypatch.setattr("refactorq.core.execution.service.CodexGuardedApplier.apply", fake_apply)

    result = RefactorQService().apply(tmp_path, "balanced")

    assert result.status == "no_changes"
    assert result.applied_candidates == []
    assert result.skipped_candidates[0].reason == "guarded Codex flow touched files outside the allowed candidate scope"
    assert sample.read_text(encoding="utf-8") == original
    assert helper.read_text(encoding="utf-8") == "print('helper')\n"

def test_balanced_apply_rejects_guarded_candidate_id_expansion(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    sample = tmp_path / "sample.py"
    original = _long_python_function()
    sample.write_text(original, encoding="utf-8")

    monkeypatch.setattr("refactorq.core.execution.service.CodexGuardedApplier.is_available", lambda self: True)

    def fake_apply(self: CodexGuardedApplier, root: Path, candidate: Candidate) -> GuardedApplyResult:
        (root / candidate.files[0]).write_text("def very_long_function():\n    return 39\n", encoding="utf-8")
        return GuardedApplyResult(
            status="applied",
            candidateIds=[candidate.id, "synthetic-new-candidate"],
            touchedFiles=[candidate.files[0]],
            summary=["declared extra candidate id"],
            details={},
        )


    monkeypatch.setattr("refactorq.core.execution.service.CodexGuardedApplier.apply", fake_apply)

    result = RefactorQService().apply(tmp_path, "balanced")

    assert result.status == "no_changes"
    assert result.applied_candidates == []
    assert result.skipped_candidates[0].reason == "guarded Codex response declared candidateIds outside the selected candidate scope"
    assert sample.read_text(encoding="utf-8") == original

def test_balanced_apply_rejects_guarded_candidate_id_omission(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    sample = tmp_path / "sample.py"
    original = _long_python_function()
    sample.write_text(original, encoding="utf-8")

    monkeypatch.setattr("refactorq.core.execution.service.CodexGuardedApplier.is_available", lambda self: True)

    def fake_apply(self: CodexGuardedApplier, root: Path, candidate: Candidate) -> GuardedApplyResult:
        (root / candidate.files[0]).write_text("def very_long_function():\n    return 39\n", encoding="utf-8")
        return GuardedApplyResult(
            status="applied",
            touchedFiles=[candidate.files[0]],
            summary=["omitted candidate ids"],
            details={},
        )

    monkeypatch.setattr("refactorq.core.execution.service.CodexGuardedApplier.apply", fake_apply)

    result = RefactorQService().apply(tmp_path, "balanced")

    assert result.status == "no_changes"
    assert result.applied_candidates == []
    assert result.skipped_candidates[0].reason == "guarded Codex response omitted candidateIds for the selected candidate scope"
    assert sample.read_text(encoding="utf-8") == original



def test_balanced_apply_rejects_guarded_no_change_with_repo_diff(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    sample = tmp_path / "sample.py"
    original = _long_python_function()
    sample.write_text(original, encoding="utf-8")

    monkeypatch.setattr("refactorq.core.execution.service.CodexGuardedApplier.is_available", lambda self: True)

    def fake_apply(self: CodexGuardedApplier, root: Path, candidate: Candidate) -> GuardedApplyResult:
        (root / candidate.files[0]).write_text("def very_long_function():\n    return 39\n", encoding="utf-8")
        return GuardedApplyResult(
            status="no_change",
            candidateIds=[candidate.id],
            touchedFiles=[candidate.files[0]],
            summary=["claimed no change"],
            details={},
        )

    monkeypatch.setattr("refactorq.core.execution.service.CodexGuardedApplier.apply", fake_apply)

    result = RefactorQService().apply(tmp_path, "balanced")

    assert result.status == "no_changes"
    assert result.applied_candidates == []
    assert result.skipped_candidates[0].reason == "guarded Codex response reported no_change despite modifying the repo"
    assert sample.read_text(encoding="utf-8") == original



def test_balanced_apply_rejects_guarded_touched_files_mismatch(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    sample = tmp_path / "sample.py"
    original = _long_python_function()
    sample.write_text(original, encoding="utf-8")

    monkeypatch.setattr("refactorq.core.execution.service.CodexGuardedApplier.is_available", lambda self: True)

    def fake_apply(self: CodexGuardedApplier, root: Path, candidate: Candidate) -> GuardedApplyResult:
        (root / candidate.files[0]).write_text("def very_long_function():\n    return 39\n", encoding="utf-8")
        return GuardedApplyResult(
            status="applied",
            candidateIds=[candidate.id],
            touchedFiles=[],
            summary=["changed file but omitted touchedFiles"],
            details={},
        )

    monkeypatch.setattr("refactorq.core.execution.service.CodexGuardedApplier.apply", fake_apply)

    result = RefactorQService().apply(tmp_path, "balanced")

    assert result.status == "no_changes"
    assert result.applied_candidates == []
    assert result.skipped_candidates[0].reason == "guarded Codex response touchedFiles did not match the actual changed files"
    assert sample.read_text(encoding="utf-8") == original



def test_balanced_apply_rejects_guarded_same_file_broad_rewrite(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    sample = tmp_path / "sample.py"
    original = _wide_inline_python_module()
    sample.write_text(original, encoding="utf-8")

    monkeypatch.setattr("refactorq.core.execution.service.CodexGuardedApplier.is_available", lambda self: True)

    def fake_apply(self: CodexGuardedApplier, root: Path, candidate: Candidate) -> GuardedApplyResult:
        (root / candidate.files[0]).write_text('print("rewritten")\n', encoding="utf-8")
        return GuardedApplyResult(
            status="applied",
            candidateIds=[candidate.id],
            touchedFiles=[candidate.files[0]],
            summary=["rewrote entire file"],
            details={},
        )

    monkeypatch.setattr("refactorq.core.execution.service.CodexGuardedApplier.apply", fake_apply)

    result = RefactorQService().apply(tmp_path, "balanced")

    assert result.status == "no_changes"
    assert result.applied_candidates == []
    assert result.skipped_candidates[0].reason.startswith("guarded Codex flow exceeded the same-file diff safety budget before verification")
    assert sample.read_text(encoding="utf-8") == original


def test_balanced_apply_skips_guarded_candidate_after_same_file_auto_edit(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text("import os\n\n" + _long_python_function(), encoding="utf-8")

    monkeypatch.setattr("refactorq.core.execution.service.CodexGuardedApplier.is_available", lambda self: True)

    def fake_apply(self: CodexGuardedApplier, root: Path, candidate: Candidate) -> GuardedApplyResult:
        raise AssertionError("guarded apply should be skipped after same-file auto edit")

    monkeypatch.setattr("refactorq.core.execution.service.CodexGuardedApplier.apply", fake_apply)

    result = RefactorQService().apply(tmp_path, "balanced")

    assert result.status == "applied"
    assert [candidate.kind for candidate in result.applied_candidates] == ["unused_import"]
    assert [note.candidate.kind for note in result.skipped_candidates] == ["extract_function"]
    assert result.skipped_candidates[0].reason == "guarded candidate anchors require re-scan after earlier same-file edits"
    assert sample.read_text(encoding="utf-8") == "\n" + _long_python_function()


def test_balanced_apply_skips_later_guarded_candidate_after_same_file_guarded_edit(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(_wide_inline_python_module() + "\n" + _long_python_function(), encoding="utf-8")

    first = Candidate.model_validate(
        {
            "id": "extract-same-file",
            "kind": "extract_function",
            "title": "Extract helper",
            "description": "Synthetic same-file guarded extract candidate",
            "language": "python",
            "scope": "local",
            "source": ["static"],
            "files": ["sample.py"],
            "symbols": ["very_long_function"],
            "anchorRegions": [{"file": "sample.py", "startLine": 20, "endLine": 60}],
            "applyModeHint": "guarded",
            "requiredChecks": ["parse", "lint", "typecheck", "unit_test"],
        }
    )
    second = Candidate.model_validate(
        {
            "id": "inline-same-file",
            "kind": "inline_function",
            "title": "Inline helper",
            "description": "Synthetic same-file guarded inline candidate",
            "language": "python",
            "scope": "module",
            "source": ["static"],
            "files": ["sample.py"],
            "symbols": ["_normalize_value"],
            "anchorRegions": [{"file": "sample.py", "startLine": 1, "endLine": 3}],
            "applyModeHint": "guarded",
            "requiredChecks": ["parse", "lint", "typecheck", "unit_test"],
        }
    )
    plan = PlanResult(
        mode="balanced",
        repo=RepoSnapshot(
            root=str(tmp_path),
            pythonFiles=1,
            typescriptFiles=0,
            javascriptFiles=0,
            manifests=RepoManifestMap(),
            toolchain=[],
            languages=["python"],
            mixedLanguage=False,
            boundaryArtifacts=[],
        ),
        adapterNames=["python"],
        selectedCandidates=[first, second],
        excludedCandidates=[],
        edges=[],
        requiredChecks=["parse", "lint", "typecheck", "unit_test"],
        candidateCount=2,
        selectedCount=2,
        excludedCount=0,
    )

    monkeypatch.setattr("refactorq.core.execution.service.CodexGuardedApplier.is_available", lambda self: True)
    calls: list[str] = []

    def fake_apply(self: CodexGuardedApplier, root: Path, candidate: Candidate) -> GuardedApplyResult:
        calls.append(candidate.kind)
        target = root / candidate.files[0]
        target.write_text(
            _wide_inline_python_module()
            + "\n"
            + "def _very_long_function_impl():\n    return 39\n\n\n"
            + "def very_long_function():\n    return _very_long_function_impl()\n",
            encoding="utf-8",
        )
        return GuardedApplyResult(
            status="applied",
            candidateIds=[candidate.id],
            touchedFiles=[candidate.files[0]],
            summary=["first guarded candidate applied"],
            details={"candidate": candidate.id},
        )

    monkeypatch.setattr("refactorq.core.execution.service.CodexGuardedApplier.apply", fake_apply)

    result = _apply_plan_internal(tmp_path, plan)

    assert result.status == "applied"
    assert calls == ["extract_function"]
    assert [candidate.kind for candidate in result.applied_candidates] == ["extract_function"]
    assert [note.candidate.kind for note in result.skipped_candidates] == ["inline_function"]
    assert result.skipped_candidates[0].reason == "guarded candidate anchors require re-scan after earlier same-file edits"

def test_balanced_apply_rejects_guarded_timeout(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    sample = tmp_path / "sample.py"
    original = _long_python_function()
    sample.write_text(original, encoding="utf-8")

    monkeypatch.setattr("refactorq.core.execution.service.CodexGuardedApplier.is_available", lambda self: True)
    observed: dict[str, int | None] = {}

    def fake_run(*args: object, **kwargs: object) -> object:
        timeout_arg = kwargs.get("timeout")
        timeout_value = float(cast(int | float, timeout_arg))
        observed["timeout"] = int(timeout_value)
        command = cast(str | list[str], args[0] if args else "codex")
        raise subprocess.TimeoutExpired(command, timeout_value)

    monkeypatch.setattr("refactorq.agents.codex.adapter.subprocess.run", fake_run)

    result = RefactorQService().apply(tmp_path, "balanced")

    assert result.status == "no_changes"
    assert result.applied_candidates == []
    assert result.skipped_candidates[0].reason == "Codex guarded execution timed out"
    assert observed["timeout"] is not None and observed["timeout"] > 0
    assert sample.read_text(encoding="utf-8") == original


def test_balanced_apply_rejects_missing_guarded_output_file(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    sample = tmp_path / "sample.py"
    original = _long_python_function()
    sample.write_text(original, encoding="utf-8")

    monkeypatch.setattr("refactorq.core.execution.service.CodexGuardedApplier.is_available", lambda self: True)

    def fake_run(*args: object, **kwargs: object) -> object:
        command = cast(str | list[str], args[0] if args else ["codex"])
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("refactorq.agents.codex.adapter.subprocess.run", fake_run)

    result = RefactorQService().apply(tmp_path, "balanced")

    assert result.status == "no_changes"
    assert result.applied_candidates == []
    assert result.skipped_candidates[0].reason == "Codex guarded execution did not produce structured output"
    assert sample.read_text(encoding="utf-8") == original



def test_verify_reports_python_syntax_failures(tmp_path: Path) -> None:
    broken = tmp_path / "broken.py"
    broken.write_text("def nope(:\n    pass\n", encoding="utf-8")

    result = RefactorQService().verify(tmp_path)

    assert result.status == "failed"
    assert result.checks[0].name == "python_parse"
    assert result.checks[0].status == "failed"
    assert "broken.py" in result.checks[0].evidence[0]



def test_run_rolls_back_when_verification_fails(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    sample = tmp_path / "sample.py"
    original = "import os\n\nprint('hi')\n"
    sample.write_text(original, encoding="utf-8")

    def fail_verify(
        root: Path,
        *,
        required_checks: list[str] | None = None,
        candidates: list[Candidate] | None = None,
    ) -> VerificationResult:
        return VerificationResult(
            status="failed",
            checks=[
                VerificationCheckResult(
                    name="python_parse",
                    kind="parse",
                    status="failed",
                    evidence=["forced failure"],
                    details={},
                )
            ],
        )

    monkeypatch.setattr("refactorq.core.execution.service.verify_repo", fail_verify)


    result = RefactorQService().run(tmp_path, "safe")

    assert result.status == "rolled_back"
    assert result.rollback_applied is True
    assert result.repair.status == "not_needed"
    assert sample.read_text(encoding="utf-8") == original



def test_run_repairs_guarded_changes_before_succeeding(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    sample = tmp_path / "sample.py"
    original = _long_python_function()
    sample.write_text(original, encoding="utf-8")

    monkeypatch.setattr("refactorq.core.execution.service.CodexGuardedApplier.is_available", lambda self: True)

    def fake_apply(self: CodexGuardedApplier, root: Path, candidate: Candidate) -> GuardedApplyResult:
        (root / candidate.files[0]).write_text("def very_long_function(:\n    pass\n", encoding="utf-8")
        return GuardedApplyResult(
            status="applied",
            candidateIds=[candidate.id],
            touchedFiles=[candidate.files[0]],
            summary=["broke syntax"],
            details={},
        )

    def fake_repair(
        self: CodexGuardedApplier,
        root: Path,
        candidates: list[Candidate],
        verification: VerificationResult,
    ) -> GuardedApplyResult:
        (root / candidates[0].files[0]).write_text(
            "def _very_long_function_impl():\n    return 39\n\n\ndef very_long_function():\n    return _very_long_function_impl()\n",
            encoding="utf-8",
        )
        return GuardedApplyResult(
            status="applied",
            candidateIds=[candidate.id for candidate in candidates],
            touchedFiles=[candidates[0].files[0]],
            summary=["repaired syntax"],
            details={"failure": verification.checks[0].name},
        )

    monkeypatch.setattr("refactorq.core.execution.service.CodexGuardedApplier.apply", fake_apply)
    monkeypatch.setattr("refactorq.core.execution.service.CodexGuardedApplier.repair", fake_repair)
    verification_calls = {"count": 0}

    def fake_verify_repo(root: Path, *, required_checks: list[str] | None = None, candidates: list[Candidate] | None = None) -> VerificationResult:
        verification_calls["count"] += 1
        if verification_calls["count"] == 1:
            return VerificationResult(
                status="failed",
                checks=[
                    VerificationCheckResult(name="python_parse", kind="parse", status="failed", evidence=["broke syntax"], details={}),
                ],
            )
        return VerificationResult(status="passed", checks=[])

    monkeypatch.setattr("refactorq.core.execution.service.verify_repo", fake_verify_repo)

    result = RefactorQService().run(tmp_path, "balanced")

    assert result.status == "passed"
    assert result.rollback_applied is False
    assert result.repair.status == "repaired"
    assert result.repair.attempted is True
    assert "_very_long_function_impl" in sample.read_text(encoding="utf-8")


def test_repair_rejects_guarded_partial_candidate_id_scope(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    first_path = tmp_path / "first.py"
    second_path = tmp_path / "second.py"
    first_path.write_text("print('first')\n", encoding="utf-8")
    second_path.write_text("print('second')\n", encoding="utf-8")

    first = Candidate.model_validate(
        {
            "id": "py-inline-first",
            "kind": "inline_function",
            "title": "Inline first",
            "description": "Synthetic first guarded candidate",
            "language": "python",
            "scope": "module",
            "source": ["static"],
            "files": ["first.py"],
            "symbols": ["_first"],
            "anchorRegions": [{"file": "first.py", "startLine": 1, "endLine": 1}],
            "applyModeHint": "guarded",
            "requiredChecks": ["parse", "lint"],
        }
    )
    second = Candidate.model_validate(
        {
            "id": "py-inline-second",
            "kind": "inline_function",
            "title": "Inline second",
            "description": "Synthetic second guarded candidate",
            "language": "python",
            "scope": "module",
            "source": ["static"],
            "files": ["second.py"],
            "symbols": ["_second"],
            "anchorRegions": [{"file": "second.py", "startLine": 1, "endLine": 1}],
            "applyModeHint": "guarded",
            "requiredChecks": ["parse", "lint"],
        }
    )
    verification = VerificationResult(
        status="failed",
        checks=[
            VerificationCheckResult(
                name="python_parse",
                kind="parse",
                status="failed",
                evidence=["first.py:1:1 invalid syntax"],
                details={},
            )
        ],
    )

    monkeypatch.setattr("refactorq.core.execution.service.CodexGuardedApplier.is_available", lambda self: True)

    def fake_repair(
        self: CodexGuardedApplier,
        root: Path,
        candidates: list[Candidate],
        verification: VerificationResult,
    ) -> GuardedApplyResult:
        (root / candidates[0].files[0]).write_text("print('repaired')\n", encoding="utf-8")
        return GuardedApplyResult(
            status="applied",
            candidateIds=[candidates[0].id],
            touchedFiles=[candidates[0].files[0]],
            summary=["declared only one candidate id"],
            details={},
        )

    monkeypatch.setattr("refactorq.core.execution.service.CodexGuardedApplier.repair", fake_repair)

    attempt = _repair_guarded_changes(tmp_path, [first, second], verification, CodexGuardedApplier())

    assert attempt.repaired is False
    assert attempt.result.status == "failed"
    assert attempt.result.reason == "guarded Codex repair candidateIds did not match the selected candidate scope"
    assert first_path.read_text(encoding="utf-8") == "print('first')\n"
    assert second_path.read_text(encoding="utf-8") == "print('second')\n"


def test_run_rolls_back_guarded_changes_when_repair_cannot_fix(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    sample = tmp_path / "sample.py"
    original = _long_python_function()
    sample.write_text(original, encoding="utf-8")

    monkeypatch.setattr("refactorq.core.execution.service.CodexGuardedApplier.is_available", lambda self: True)

    def fake_apply(self: CodexGuardedApplier, root: Path, candidate: Candidate) -> GuardedApplyResult:
        (root / candidate.files[0]).write_text("def very_long_function(:\n    pass\n", encoding="utf-8")
        return GuardedApplyResult(
            status="applied",
            candidateIds=[candidate.id],
            touchedFiles=[candidate.files[0]],
            summary=["broke syntax"],
            details={},
        )

    def fake_repair(
        self: CodexGuardedApplier,
        root: Path,
        candidates: list[Candidate],
        verification: VerificationResult,
    ) -> GuardedApplyResult:
        return GuardedApplyResult(status="no_change", candidateIds=[candidate.id for candidate in candidates], touchedFiles=[], summary=["could not repair"], details={})

    monkeypatch.setattr("refactorq.core.execution.service.CodexGuardedApplier.apply", fake_apply)
    monkeypatch.setattr("refactorq.core.execution.service.CodexGuardedApplier.repair", fake_repair)

    result = RefactorQService().run(tmp_path, "balanced")

    assert result.status == "rolled_back"
    assert result.rollback_applied is True
    assert result.repair.status == "skipped"
    assert result.repair.reason == "could not repair"
    assert sample.read_text(encoding="utf-8") == original


def test_run_rolls_back_when_guarded_repair_times_out(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    sample = tmp_path / "sample.py"
    original = _long_python_function()
    sample.write_text(original, encoding="utf-8")

    monkeypatch.setattr("refactorq.core.execution.service.CodexGuardedApplier.is_available", lambda self: True)

    def fake_apply(self: CodexGuardedApplier, root: Path, candidate: Candidate) -> GuardedApplyResult:
        (root / candidate.files[0]).write_text("def very_long_function(:\n    pass\n", encoding="utf-8")
        return GuardedApplyResult(
            status="applied",
            candidateIds=[candidate.id],
            touchedFiles=[candidate.files[0]],
            summary=["broke syntax"],
            details={},
        )

    def fake_repair(
        self: CodexGuardedApplier,
        root: Path,
        candidates: list[Candidate],
        verification: VerificationResult,
    ) -> GuardedApplyResult:
        raise subprocess.TimeoutExpired("codex", 120)

    monkeypatch.setattr("refactorq.core.execution.service.CodexGuardedApplier.apply", fake_apply)
    monkeypatch.setattr("refactorq.core.execution.service.CodexGuardedApplier.repair", fake_repair)

    result = RefactorQService().run(tmp_path, "balanced")

    assert result.status == "rolled_back"
    assert result.rollback_applied is True
    assert result.repair.status == "failed"
    assert result.repair.reason == "Codex guarded execution timed out"
    assert sample.read_text(encoding="utf-8") == original


def test_run_rolls_back_when_guarded_repair_reports_no_change_with_repo_diff(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    sample = tmp_path / "sample.py"
    original = _long_python_function()
    sample.write_text(original, encoding="utf-8")

    monkeypatch.setattr("refactorq.core.execution.service.CodexGuardedApplier.is_available", lambda self: True)

    def fake_apply(self: CodexGuardedApplier, root: Path, candidate: Candidate) -> GuardedApplyResult:
        (root / candidate.files[0]).write_text("def very_long_function(:\n    pass\n", encoding="utf-8")
        return GuardedApplyResult(
            status="applied",
            candidateIds=[candidate.id],
            touchedFiles=[candidate.files[0]],
            summary=["broke syntax"],
            details={},
        )

    def fake_repair(
        self: CodexGuardedApplier,
        root: Path,
        candidates: list[Candidate],
        verification: VerificationResult,
    ) -> GuardedApplyResult:
        (root / candidates[0].files[0]).write_text(
            "def _very_long_function_impl():\n    return 39\n\n\ndef very_long_function():\n    return _very_long_function_impl()\n",
            encoding="utf-8",
        )
        return GuardedApplyResult(
            status="no_change",
            candidateIds=[candidate.id for candidate in candidates],
            touchedFiles=[candidates[0].files[0]],
            summary=["claimed no change"],
            details={"failure": verification.checks[0].name},
        )

    monkeypatch.setattr("refactorq.core.execution.service.CodexGuardedApplier.apply", fake_apply)
    monkeypatch.setattr("refactorq.core.execution.service.CodexGuardedApplier.repair", fake_repair)

    result = RefactorQService().run(tmp_path, "balanced")

    assert result.status == "rolled_back"
    assert result.rollback_applied is True
    assert result.repair.status == "failed"
    assert result.repair.reason == "guarded Codex repair reported no_change despite modifying the repo"
    assert sample.read_text(encoding="utf-8") == original



def test_run_rolls_back_when_guarded_repair_touched_files_mismatch(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    sample = tmp_path / "sample.py"
    original = _long_python_function()
    sample.write_text(original, encoding="utf-8")

    monkeypatch.setattr("refactorq.core.execution.service.CodexGuardedApplier.is_available", lambda self: True)

    def fake_apply(self: CodexGuardedApplier, root: Path, candidate: Candidate) -> GuardedApplyResult:
        (root / candidate.files[0]).write_text("def very_long_function(:\n    pass\n", encoding="utf-8")
        return GuardedApplyResult(
            status="applied",
            candidateIds=[candidate.id],
            touchedFiles=[candidate.files[0]],
            summary=["broke syntax"],
            details={},
        )

    def fake_repair(
        self: CodexGuardedApplier,
        root: Path,
        candidates: list[Candidate],
        verification: VerificationResult,
    ) -> GuardedApplyResult:
        (root / candidates[0].files[0]).write_text(
            "def _very_long_function_impl():\n    return 39\n\n\ndef very_long_function():\n    return _very_long_function_impl()\n",
            encoding="utf-8",
        )
        return GuardedApplyResult(
            status="applied",
            candidateIds=[candidate.id for candidate in candidates],
            touchedFiles=[],
            summary=["repaired syntax"],
            details={"failure": verification.checks[0].name},
        )

    monkeypatch.setattr("refactorq.core.execution.service.CodexGuardedApplier.apply", fake_apply)
    monkeypatch.setattr("refactorq.core.execution.service.CodexGuardedApplier.repair", fake_repair)

    result = RefactorQService().run(tmp_path, "balanced")

    assert result.status == "rolled_back"
    assert result.rollback_applied is True
    assert result.repair.status == "failed"
    assert result.repair.reason == "guarded Codex repair touchedFiles did not match the actual changed files"
    assert sample.read_text(encoding="utf-8") == original


def test_guarded_repair_passes_timeout_to_subprocess(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(_long_python_function(), encoding="utf-8")

    monkeypatch.setattr("refactorq.agents.codex.adapter.shutil.which", lambda _: "codex")
    observed: dict[str, int | None] = {}

    def fake_run(*args: object, **kwargs: object) -> object:
        timeout_arg = kwargs.get("timeout")
        timeout_value = float(cast(int | float, timeout_arg))
        observed["timeout"] = int(timeout_value)
        command = cast(str | list[str], args[0] if args else "codex")
        raise subprocess.TimeoutExpired(command, timeout_value)

    monkeypatch.setattr("refactorq.agents.codex.adapter.subprocess.run", fake_run)

    candidate = Candidate.model_validate(
        {
            "id": "py-extract-timeout",
            "kind": "extract_function",
            "title": "Extract helper",
            "description": "Timeout coverage",
            "language": "python",
            "scope": "local",
            "source": ["static"],
            "files": ["sample.py"],
            "symbols": ["very_long_function"],
            "anchorRegions": [{"file": "sample.py", "startLine": 1, "endLine": 1}],
            "applyModeHint": "guarded",
            "requiredChecks": ["parse", "lint", "typecheck", "unit_test"],
        }
    )
    verification = VerificationResult(
        status="failed",
        checks=[
            VerificationCheckResult(
                name="python_parse",
                kind="parse",
                status="failed",
                evidence=["forced failure"],
                details={},
            )
        ],
    )

    try:
        CodexGuardedApplier().repair(tmp_path, [candidate], verification)
        raise AssertionError("expected TimeoutExpired")
    except subprocess.TimeoutExpired:
        pass

    assert observed["timeout"] is not None and observed["timeout"] > 0



def test_run_creates_git_branch_and_commit_when_workspace_is_clean(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text("import os\n\nprint('hi')\n", encoding="utf-8")
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.name", "RefactorQ Test")
    _git(tmp_path, "config", "user.email", "refactorq@example.com")
    _git(tmp_path, "add", "sample.py")
    _git(tmp_path, "commit", "-m", "baseline")

    result = RefactorQService().run(tmp_path, "safe")

    assert result.status == "passed"
    assert result.git.enabled is True
    assert result.git.execution_branch is not None
    assert result.git.execution_branch.startswith("refactorq/safe-")
    assert result.git.commit_sha is not None
    assert _git(tmp_path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == result.git.execution_branch
    assert _git(tmp_path, "show", "--stat", "--oneline", "-1").stdout.startswith(result.git.commit_sha[:7])


def test_run_no_changes_preserves_not_applicable_readiness(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text("print('ok')\n", encoding="utf-8")

    result = RefactorQService().run(tmp_path, "safe")

    assert result.status == "no_changes"
    assert result.verification.readiness.ready is True
    assert result.verification.readiness.proof_status == "not_applicable"
    assert result.verification.proof_records == []
    assert result.verification.status == "skipped"



def test_report_summarizes_supported_execution_candidates(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text("import os\n\nprint('hi')\n", encoding="utf-8")

    result = RefactorQService().report(tmp_path, "report")

    assert result.execution_support.supported_candidates == 1
    assert result.execution_support.supported_auto_candidates == 1
    assert result.execution_support.supported_guarded_candidates == 0
    assert result.execution_support.unsupported_candidates == 0
    assert result.execution_support.applied_candidate_kinds == ["unused_import"]
    assert result.execution_support.git_branching_supported is False


def test_report_surfaces_boundary_execution_summary(tmp_path: Path) -> None:
    backend = tmp_path / "backend"
    frontend = tmp_path / "frontend"
    backend.mkdir()
    frontend.mkdir()
    (tmp_path / "openapi.yaml").write_text(_openapi_contract(), encoding="utf-8")
    (backend / "api.py").write_text('import os\n\nROUTE = "/items"\nprint(ROUTE)\n', encoding="utf-8")
    (frontend / "client.ts").write_text('const endpoint = "/items";\nconsole.log(endpoint);\n', encoding="utf-8")

    result = RefactorQService().report(tmp_path, "report")

    assert result.boundary_execution.cross_language_candidates >= 1
    assert result.boundary_execution.boundary_sensitive_candidates >= 1
    assert result.boundary_execution.blocked_boundary_candidates == 0
    assert result.boundary_execution.contract_artifacts == ["openapi.yaml"]
    assert result.boundary_execution.highest_impact in {"low", "medium", "high"}
    assert result.boundary_execution.ready_boundary_candidates >= 1
    assert result.boundary_execution.contract_ready_candidates >= 1
    assert result.boundary_execution.contract_blocked_candidates == 0
    assert result.boundary_execution.blocked_reasons == []
    assert result.verification_plan.required_checks == ["parse", "lint", "typecheck", "integration_test", "build"]
    assert result.verification_plan.missing_required_checks == []
    boundary_candidate = next(
        candidate
        for candidate in result.verification_plan.boundary_candidates
        if candidate.candidate_id.startswith("py-unused-import-backend/api.py")
    )
    assert boundary_candidate.ready is True
    assert boundary_candidate.producer_side == ["backend/api.py"]
    assert boundary_candidate.consumer_side == ["frontend/client.ts"]
    assert boundary_candidate.contract_artifacts == ["openapi.yaml"]
    assert not any(
        candidate.candidate_id == "boundary-review-openapi-yaml"
        for candidate in result.verification_plan.boundary_candidates
    )


def test_verify_reports_boundary_contract_check_for_mixed_repo(tmp_path: Path) -> None:
    backend = tmp_path / "backend"
    frontend = tmp_path / "frontend"
    backend.mkdir()
    frontend.mkdir()
    (tmp_path / "openapi.yaml").write_text(_openapi_contract(), encoding="utf-8")
    (backend / "api.py").write_text('print("/items")\n', encoding="utf-8")
    (frontend / "client.ts").write_text('console.log("/items");\n', encoding="utf-8")

    result = RefactorQService().verify(tmp_path)

    boundary_check = next(check for check in result.checks if check.name == "boundary_contracts")
    assert boundary_check.status == "passed"
    assert boundary_check.kind == "build"
    assert "openapi.yaml" in boundary_check.evidence[0]


def test_verify_exposes_not_applicable_readiness_for_single_language_repo(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    sample = tmp_path / "pkg.py"
    sample.write_text("print('ok')\n", encoding="utf-8")

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        command = cast(list[str], args[0])
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr("refactorq.core.verification.command_checks.subprocess.run", fake_run)

    result = RefactorQService().verify(tmp_path)

    assert result.status == "passed"
    assert result.readiness.ready is True
    assert result.readiness.proof_status == "not_applicable"
    assert result.readiness.missing_predicates == []
    assert result.proof_records == []



def test_verify_fails_on_invalid_json_boundary_artifact(tmp_path: Path) -> None:
    backend = tmp_path / "backend"
    frontend = tmp_path / "frontend"
    backend.mkdir()
    frontend.mkdir()
    (tmp_path / "schema.json").write_text("{not-json}\n", encoding="utf-8")
    (backend / "api.py").write_text("print('ok')\n", encoding="utf-8")
    (frontend / "client.ts").write_text("console.log('ok');\n", encoding="utf-8")

    result = RefactorQService().verify(tmp_path)

    assert result.status == "failed"
    boundary_check = next(check for check in result.checks if check.name == "boundary_contracts")
    assert boundary_check.status == "failed"
    assert "schema.json" in boundary_check.evidence[0]


def test_verify_exposes_proven_boundary_readiness_for_mixed_repo(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    backend = tmp_path / "backend"
    frontend = tmp_path / "frontend"
    backend.mkdir()
    frontend.mkdir()
    (tmp_path / "openapi.yaml").write_text(_openapi_contract(), encoding="utf-8")
    (backend / "api.py").write_text('import os\n\nROUTE = "/items"\nprint(ROUTE)\n', encoding="utf-8")
    (frontend / "client.ts").write_text('const endpoint = "/items";\nconsole.log(endpoint);\n', encoding="utf-8")

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        command = cast(list[str], args[0])
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr("refactorq.core.verification.command_checks.subprocess.run", fake_run)
    monkeypatch.setattr(
        "refactorq.core.verification.service.TypeScriptAdapter.verify",
        lambda self, root: [
            VerificationCheckResult(
                name="typescript_parse",
                kind="parse",
                status="passed",
                evidence=["parsed 1 TypeScript/JavaScript files"],
                details={},
            )
        ],
    )

    scan_result = RefactorQService().scan(tmp_path)
    candidate = next(item for item in scan_result.candidates if item.id.startswith("py-unused-import-backend/api.py"))
    result = verify_repo(tmp_path, required_checks=["build", "integration_test"], candidates=[candidate])

    assert result.status == "passed"
    assert result.readiness.ready is True
    assert result.readiness.proof_status == "proven"
    assert result.readiness.proof_refs
    assert result.proof_records

def test_verify_runs_python_toolchain_commands(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    sample = tmp_path / "pkg.py"
    sample.write_text("print('ok')\n", encoding="utf-8")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_pkg.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    commands: list[list[str]] = []

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        command = cast(list[str], args[0])
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr("refactorq.core.verification.command_checks.subprocess.run", fake_run)

    result = RefactorQService().verify(tmp_path)

    assert result.status == "passed"
    assert [check.name for check in result.checks[:4]] == [
        "python_parse",
        "python_lint",
        "python_typecheck",
        "python_unit_tests",
    ]
    assert [command[:3] for command in commands] == [
        [sys.executable, "-m", "ruff"],
        [sys.executable, "-m", "mypy"],
        [sys.executable, "-m", "pytest"],
    ]


def test_verify_runs_typescript_package_scripts(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    (tmp_path / "sample.ts").write_text("console.log('ok');\n", encoding="utf-8")
    (tmp_path / "package.json").write_text(
        '{"scripts":{"ts:check":"tsc --noEmit","ts:build":"tsc"}}',
        encoding="utf-8",
    )

    commands: list[list[str]] = []

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        command = cast(list[str], args[0])
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr("refactorq.core.verification.command_checks.subprocess.run", fake_run)
    monkeypatch.setattr(
        "refactorq.core.verification.service.TypeScriptAdapter.verify",
        lambda self, root: [
            VerificationCheckResult(
                name="typescript_parse",
                kind="parse",
                status="passed",
                evidence=["parsed 1 TypeScript/JavaScript files"],
                details={},
            )
        ],
    )

    result = RefactorQService().verify(tmp_path)

    assert result.status == "passed"
    assert any(check.name == "typescript_typecheck" for check in result.checks)
    assert any(check.name == "typescript_build" for check in result.checks)
    assert commands == [[_npm_command(), "run", "ts:check"], [_npm_command(), "run", "ts:build"]]

def test_apply_command_emits_real_execution_payload(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text("import os\n\nprint('hi')\n", encoding="utf-8")

    result = runner.invoke(app, ["apply", str(tmp_path), "--mode", "safe"])

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["status"] == "applied"
    assert payload["changedFiles"]

def test_run_reports_optimizer_rejected_no_batch_without_apply(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text("print('ok')\n", encoding="utf-8")
    rejected_candidate = Candidate.model_validate(
        {
            "id": "candidate-a",
            "kind": "extract_function",
            "title": "Candidate A",
            "description": "Rejected optimizer candidate",
            "language": "python",
            "scope": "local",
            "source": ["static"],
            "files": ["sample.py"],
            "symbols": ["very_long_function"],
            "anchorRegions": [{"file": "sample.py", "startLine": 1, "endLine": 1}],
            "applyModeHint": "guarded",
            "requiredChecks": ["parse", "lint"],
        }
    )
    plan = PlanResult(
        mode="balanced",
        repo=RepoSnapshot(
            root=str(tmp_path),
            pythonFiles=1,
            typescriptFiles=0,
            javascriptFiles=0,
            manifests=RepoManifestMap(),
            toolchain=[],
            languages=["python"],
            mixedLanguage=False,
            boundaryArtifacts=[],
        ),
        adapterNames=["python"],
        selectedCandidates=[],
        excludedCandidates=[],
        edges=[],
        requiredChecks=[],
        candidateCount=1,
        selectedCount=0,
        excludedCount=0,
        selectionSource="optimizer_rejected_no_batch",
        solverProposal=SolverProposal(
            repo=RepoSnapshot(
                root=str(tmp_path),
                pythonFiles=1,
                typescriptFiles=0,
                javascriptFiles=0,
                manifests=RepoManifestMap(),
                toolchain=[],
                languages=["python"],
                mixedLanguage=False,
                boundaryArtifacts=[],
            ),
            adapterNames=["python"],
            candidates=[rejected_candidate],
            backend="qubo_local_search",
            selectedCandidateIds=["candidate-a"],
            objectiveScore=1.0,
            hardConstraintStatus="satisfied",
            diagnostics={},
        ),
        proposalRevalidation=ProposalRevalidation(
            status="rejected",
            rejectionReasons=["planner revalidation rejected the optimizer proposal"],
            finalSelectedCandidateIds=[],
        ),
    )

    result = run_plan(tmp_path, plan)

    assert result.status == "rejected_no_batch"
    assert result.executed_selection_source == "optimizer_rejected_no_batch"
    assert result.apply.status == "rejected_no_batch"
    assert result.apply.applied_candidates == []
    assert result.apply.changed_files == []
    assert result.verification.status == "skipped"


def test_run_forwards_authoritative_required_checks_to_verifier(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text("import os\n\nprint('ok')\n", encoding="utf-8")

    recorded: dict[str, object] = {}

    def fake_verify_repo(root: Path, *, required_checks: list[str] | None = None, candidates: list[Candidate] | None = None) -> VerificationResult:
        recorded["root"] = root
        recorded["required_checks"] = list(required_checks or [])
        recorded["candidate_ids"] = [candidate.id for candidate in candidates or []]
        return VerificationResult(status="passed", checks=[])

    monkeypatch.setattr("refactorq.core.execution.service.verify_repo", fake_verify_repo)

    result = RefactorQService().run(tmp_path, "safe")

    assert result.status == "passed"
    assert recorded["root"] == tmp_path
    assert recorded["required_checks"] == ["parse", "lint", "typecheck"]
    assert recorded["candidate_ids"] == ["py-unused-import-sample.py-1-os"]
    assert result.rollback_applied is False
    assert sample.read_text(encoding="utf-8") == "\nprint('ok')\n"


def test_report_preserves_rejected_optimizer_boundary_evidence(tmp_path: Path) -> None:
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / "api.py").write_text('ROUTE = "/items"\n', encoding="utf-8")
    candidate = Candidate.model_validate(
        {
            "id": "candidate-a",
            "kind": "extract_function",
            "title": "Candidate A",
            "description": "Rejected optimizer candidate",
            "language": "python",
            "scope": "module",
            "source": ["static"],
            "files": ["backend/api.py"],
            "symbols": ["very_long_function"],
            "anchorRegions": [{"file": "backend/api.py", "startLine": 1, "endLine": 1}],
            "applyModeHint": "guarded",
            "requiredChecks": ["parse", "lint", "typecheck", "build", "integration_test"],
            "boundaryImpact": {
                "crossLanguage": True,
                "contractArtifacts": ["missing-openapi.yaml"],
                "impactLevel": "low",
            },
        }
    )
    repo = RepoSnapshot(
        root=str(tmp_path),
        pythonFiles=1,
        typescriptFiles=1,
        javascriptFiles=0,
        manifests=RepoManifestMap(),
        toolchain=["python", "typescript"],
        languages=["python", "typescript"],
        mixedLanguage=True,
        boundaryArtifacts=[],
    )
    plan = PlanResult(
        mode="balanced",
        repo=repo,
        adapterNames=["python"],
        selectedCandidates=[],
        excludedCandidates=[],
        edges=[],
        requiredChecks=[],
        candidateCount=1,
        selectedCount=0,
        excludedCount=0,
        selectionSource="optimizer_rejected_no_batch",
        solverProposal=SolverProposal(
            repo=repo,
            adapterNames=["python"],
            candidates=[candidate],
            backend="qubo_local_search",
            selectedCandidateIds=[candidate.id],
            objectiveScore=1.0,
            hardConstraintStatus="satisfied",
            diagnostics={},
        ),
        proposalRevalidation=ProposalRevalidation(
            status="rejected",
            rejectionReasons=["planner revalidation rejected the optimizer proposal"],
            finalSelectedCandidateIds=[],
        ),
    )

    result = report_plan(tmp_path, plan)

    assert result.execution_support.unsupported_candidates == 1
    assert result.boundary_execution.blocked_boundary_candidates == 1
    assert "planner revalidation rejected the optimizer proposal" in result.boundary_execution.blocked_reasons
    assert result.verification_plan.boundary_candidates[0].candidate_id == "candidate-a"
    assert "artifact:missing-openapi.yaml" in result.verification_plan.missing_predicates


def test_run_preserves_verification_when_git_finalize_fails(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    sample = tmp_path / "sample.py"
    original = "import os\n\nprint('hi')\n"
    sample.write_text(original, encoding="utf-8")

    monkeypatch.setattr(
        "refactorq.core.execution.run.begin_git_execution",
        lambda root, mode: type("Ctx", (), {"execution_branch": "refactorq/safe-test"})(),
    )
    monkeypatch.setattr(
        "refactorq.core.execution.run._initial_git_result",
        lambda root: type(
            "Git",
            (),
            {
                "enabled": True,
                "available": True,
                "clean": True,
                "base_branch": "main",
                "execution_branch": None,
                "commit_sha": None,
                "reason": None,
            },
        )(),
    )
    monkeypatch.setattr(
        "refactorq.core.execution.run.finalize_git_execution",
        lambda root, context, changed_files, mode: (_ for _ in ()).throw(subprocess.CalledProcessError(1, ["git", "commit"])),
    )
    monkeypatch.setattr("refactorq.core.execution.run._abort_branch_if_needed", lambda root, context: None)
    monkeypatch.setattr(
        "refactorq.core.execution.service.verify_repo",
        lambda root, *, required_checks=None, candidates=None: VerificationResult(status="passed", checks=[]),
    )

    result = RefactorQService().run(tmp_path, "safe")

    assert result.status == "rolled_back"
    assert result.rollback_applied is True
    assert result.verification.status == "passed"
    assert result.git.reason == "git commit failed after successful verification"
    assert sample.read_text(encoding="utf-8") == original


def test_service_terminal_review_surfaces_share_authoritative_report_view(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text("import os\n", encoding="utf-8")

    scan_result = RefactorQService().scan(tmp_path)
    report_result = RefactorQService().report(tmp_path, "report")
    calls: list[Path] = []

    @contextmanager
    def fake_normalize(source: str | Path) -> Iterator[SimpleNamespace]:
        yield SimpleNamespace(
            original=str(source),
            analysis_root=tmp_path,
            kind="local",
            mutable=False,
            preserved=False,
        )

    def fake_build_report_view(root: Path) -> tuple[object, object, object]:
        calls.append(root)
        return scan_result, report_result.plan, report_result

    monkeypatch.setattr("refactorq.core.service.normalize_repo_source", fake_normalize)
    monkeypatch.setattr("refactorq.core.service._build_report_view", fake_build_report_view)

    doctor_report = RefactorQService().doctor_source(tmp_path)
    tui_payload = RefactorQService().tui_source(tmp_path)

    assert calls == [tmp_path, tmp_path]
    assert doctor_report.source == tui_payload.source
    assert doctor_report.repo == tui_payload.repo
    assert doctor_report.facts.candidate_count == len(tui_payload.candidate_rows)
    assert doctor_report.facts.selected_count == len(tui_payload.selection.selected_rows)
    assert doctor_report.facts.excluded_count == len(tui_payload.selection.excluded_rows)
    assert doctor_report.facts.optimizer_selection_source == report_result.plan.selection_source
    assert tui_payload.selection.optimizer_selection_source == report_result.plan.selection_source
