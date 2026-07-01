from __future__ import annotations

from pathlib import Path

from pytest import MonkeyPatch
from typer.testing import CliRunner

from refactorq.cli.main import app
from refactorq.core.service import RefactorQService
from refactorq.core.verification import VerificationCheckResult, VerificationResult

runner = CliRunner()



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
