from __future__ import annotations

from pathlib import Path

from pytest import MonkeyPatch
from typer.testing import CliRunner

from refactorq.agents.codex import CodexGuardedApplier, GuardedApplyResult
from refactorq.core.candidate import Candidate
from refactorq.cli.main import app
from refactorq.core.service import RefactorQService
from refactorq.core.verification import VerificationCheckResult, VerificationResult


runner = CliRunner()



def _long_python_function() -> str:
    body = ["def very_long_function():"]
    body.extend(f"    value_{index} = {index}" for index in range(40))
    body.append("    return value_39")
    body.append("")
    return "\n".join(body)



def test_apply_removes_python_unused_import(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text("import os\n\nprint('hi')\n", encoding="utf-8")

    result = RefactorQService().apply(tmp_path, "safe")

    assert result.status == "applied"
    assert result.changed_files == ["sample.py"]
    assert [candidate.id for candidate in result.applied_candidates] == ["py-unused-import-sample.py-1-os"]
    assert sample.read_text(encoding="utf-8") == "\nprint('hi')\n"



def test_apply_rewrites_named_typescript_unused_imports(tmp_path: Path) -> None:
    sample = tmp_path / "sample.ts"
    sample.write_text('import { readFile, writeFile } from "node:fs";\n\nconsole.log(writeFile);\n', encoding="utf-8")

    result = RefactorQService().apply(tmp_path, "safe")

    assert result.status == "applied"
    assert result.changed_files == ["sample.ts"]
    assert sample.read_text(encoding="utf-8") == 'import { writeFile } from "node:fs";\n\nconsole.log(writeFile);\n'



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

    def fail_verify(root: Path) -> VerificationResult:
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
    assert sample.read_text(encoding="utf-8") == original



def test_run_rolls_back_guarded_changes_on_real_parse_failure(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    sample = tmp_path / "sample.py"
    original = _long_python_function()
    sample.write_text(original, encoding="utf-8")

    monkeypatch.setattr("refactorq.core.execution.service.CodexGuardedApplier.is_available", lambda self: True)

    def fake_apply(self: CodexGuardedApplier, root: Path, candidate: Candidate) -> GuardedApplyResult:
        (root / candidate.files[0]).write_text("def very_long_function(:\n    pass\n", encoding="utf-8")
        return GuardedApplyResult(
            status="applied",
            touchedFiles=[candidate.files[0]],
            summary=["broke syntax"],
            details={},
        )

    monkeypatch.setattr("refactorq.core.execution.service.CodexGuardedApplier.apply", fake_apply)

    result = RefactorQService().run(tmp_path, "balanced")

    assert result.status == "rolled_back"
    assert result.rollback_applied is True
    assert sample.read_text(encoding="utf-8") == original



def test_report_summarizes_supported_execution_candidates(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text("import os\n\nprint('hi')\n", encoding="utf-8")

    result = RefactorQService().report(tmp_path, "report")

    assert result.execution_support.supported_candidates == 1
    assert result.execution_support.unsupported_candidates == 0
    assert result.execution_support.applied_candidate_kinds == ["unused_import"]



def test_apply_command_emits_real_execution_payload(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text("import os\n\nprint('hi')\n", encoding="utf-8")

    result = runner.invoke(app, ["apply", str(tmp_path), "--mode", "safe"])

    assert result.exit_code == 0, result.stdout
    assert '"status": "applied"' in result.stdout
    assert '"changedFiles": [' in result.stdout
