from __future__ import annotations

from pathlib import Path

import refactorq.cli.main as cli_module
import refactorq.tui as tui_module
import refactorq.tui.app as tui_app_module
from pytest import MonkeyPatch, raises
from rich.console import Console
from typer.testing import CliRunner

from refactorq.core.candidate import Candidate
from refactorq.core.execution import ReportResult
from refactorq.core.planning import PlanResult
from refactorq.core.repo.models import RepoManifestMap, RepoSnapshot
from refactorq.core.repo_source import NormalizedRepoSource
from refactorq.core.service import ScanResult
from refactorq.core.tui.builders import build_doctor_report, build_tui_review_payload
from refactorq.tui import render_doctor_report
from refactorq.tui.widgets import FilterSelection, render_candidate_panel, render_operational_panel, render_summary

runner = CliRunner()


def _candidate(*, candidate_id: str, title: str, language: str, apply_mode: str, files: list[str]) -> Candidate:
    return Candidate.model_validate(
        {
            "id": candidate_id,
            "kind": "custom",
            "title": title,
            "description": f"Review payload for {title}",
            "language": language,
            "scope": "module",
            "source": ["static"],
            "files": files,
            "symbols": [title.replace(" ", "_").lower()],
            "anchorRegions": [{"file": files[0], "startLine": 1, "endLine": 4}],
            "estimatedBenefit": {"maintainabilityGain": 0.4},
            "estimatedRisk": {"semanticRisk": 0.1, "testRisk": 0.2},
            "estimatedDiff": {"filesTouched": len(files), "linesAdded": 2, "linesModified": 1},
            "boundaryImpact": {"impactLevel": "low", "boundaryTypes": ["cli"], "producerSide": files},
            "confidence": 0.82,
            "applyModeHint": apply_mode,
            "requiredChecks": ["parse"] if language == "python" else ["build"],
            "proofIds": [f"proof:{candidate_id}"],
            "dependencies": ["shared-support"],
            "conflicts": ["manual-review"],
            "provenance": {"detectors": ["unit-test"], "evidence": [f"evidence:{candidate_id}"]},
        }
    )



def _repo_snapshot(root: Path) -> RepoSnapshot:
    return RepoSnapshot(
        root=str(root.resolve()),
        pythonFiles=1,
        typescriptFiles=1,
        javascriptFiles=0,
        manifests=RepoManifestMap(pyproject=True, packageJson=True),
        toolchain=["python", "node"],
        languages=["python", "typescript"],
        mixedLanguage=True,
        boundaryArtifacts=["openapi.yaml"],
    )



def _review_inputs(root: Path) -> tuple[NormalizedRepoSource, ScanResult, ReportResult]:
    selected = _candidate(
        candidate_id="py-custom-backend/api.py-selected",
        title="Selected candidate",
        language="python",
        apply_mode="auto",
        files=["backend/api.py"],
    )
    excluded = _candidate(
        candidate_id="ts-custom-frontend/app.ts-excluded",
        title="Excluded candidate",
        language="typescript",
        apply_mode="report_only",
        files=["frontend/app.ts"],
    )
    repo = _repo_snapshot(root)
    scan_result = ScanResult(repo=repo, adapterNames=["python", "typescript"], candidates=[selected, excluded])
    plan = PlanResult.model_validate(
        {
            "mode": "report",
            "repo": repo,
            "adapterNames": ["python", "typescript"],
            "selectedCandidates": [selected],
            "excludedCandidates": [{"candidate": excluded, "reason": "requires boundary review"}],
            "requiredChecks": ["parse", "build"],
            "candidateCount": 2,
            "selectedCount": 1,
            "excludedCount": 1,
            "selectionSource": "optimizer_qubo",
            "proposalRevalidation": {
                "status": "accepted",
                "finalSelectedCandidateIds": [selected.id],
            },
        }
    )
    report_result = ReportResult.model_validate(
        {
            "mode": "report",
            "repo": repo,
            "plan": plan,
            "executionSupport": {
                "supportedCandidates": 1,
                "supportedAutoCandidates": 1,
                "supportedGuardedCandidates": 0,
                "unsupportedCandidates": 1,
                "appliedCandidateKinds": ["custom"],
                "gitBranchingSupported": True,
                "gitReason": None,
            },
            "boundaryExecution": {
                "crossLanguageCandidates": 0,
                "boundarySensitiveCandidates": 1,
                "blockedBoundaryCandidates": 1,
                "readyBoundaryCandidates": 0,
                "contractReadyCandidates": 0,
                "contractBlockedCandidates": 1,
                "contractArtifacts": ["openapi.yaml"],
                "blockedReasons": ["requires boundary review"],
                "highestImpact": "low",
                "proofStatus": "not_applicable",
                "missingPredicates": [],
                "proofRefs": [],
            },
            "verificationPlan": {
                "requiredChecks": ["parse", "build"],
                "availableChecks": ["parse", "build"],
                "missingRequiredChecks": [],
                "boundaryCandidates": [],
                "proofStatus": "not_applicable",
                "missingPredicates": [],
                "proofRefs": [],
            },
        }
    )
    repo_source = NormalizedRepoSource(original=str(root), analysis_root=root, kind="local")
    return repo_source, scan_result, report_result



def _render_text(renderable: object) -> str:
    console = Console(record=True, width=120)
    console.print(renderable)
    return console.export_text()



def test_cli_registers_doctor_and_tui_commands() -> None:
    result = runner.invoke(cli_module.app, ["--help"])

    assert result.exit_code == 0, result.stdout
    assert "doctor" in result.stdout
    assert "tui" in result.stdout
    assert "scan" in result.stdout



def test_doctor_command_is_repo_scoped(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    repo_source, scan_result, report_result = _review_inputs(tmp_path)
    report = build_doctor_report(repo_source=repo_source, scan_result=scan_result, report_result=report_result)
    calls: dict[str, object] = {}

    def fake_doctor_source(repo: str) -> object:
        calls["repo"] = repo
        return report

    def fake_render_doctor_report(rendered_report: object, *, console: Console | None = None) -> None:
        calls["report"] = rendered_report
        calls["console"] = console

    monkeypatch.setattr(cli_module.service, "doctor_source", fake_doctor_source)
    monkeypatch.setattr(tui_module, "render_doctor_report", fake_render_doctor_report, raising=False)

    result = runner.invoke(cli_module.app, ["doctor", str(tmp_path)])

    assert result.exit_code == 0, result.stdout
    assert calls["repo"] == str(tmp_path)
    assert calls["report"] == report
    assert calls["console"] is cli_module.console


def test_tui_command_reports_optional_textual_dependency(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    repo_source, scan_result, report_result = _review_inputs(tmp_path)
    payload = build_tui_review_payload(repo_source=repo_source, scan_result=scan_result, report_result=report_result)

    monkeypatch.setattr(cli_module.service, "tui_source", lambda repo: payload)
    monkeypatch.setattr(
        tui_module,
        "launch_tui",
        lambda review_payload: (_ for _ in ()).throw(RuntimeError("Textual support requires the optional 'textual' package.")),
        raising=False,
    )

    result = runner.invoke(cli_module.app, ["tui", str(tmp_path)])

    assert result.exit_code == 1
    assert "Textual support requires the optional 'textual' package." in result.stdout



def test_build_tui_review_payload_is_report_only_and_read_only(tmp_path: Path) -> None:
    repo_source, scan_result, report_result = _review_inputs(tmp_path)
    payload = build_tui_review_payload(repo_source=repo_source, scan_result=scan_result, report_result=report_result)

    summary_text = _render_text(render_summary(payload, list(payload.candidate_rows), FilterSelection()))
    operational_text = _render_text(render_operational_panel(payload))

    assert str(payload.surface) == "tui"
    assert str(payload.operational.surface) == "tui"
    assert str(payload.operational.guidance.surface) == "tui"
    assert payload.selection.optimizer_selection_source == "optimizer_qubo"
    assert payload.drilldown is not None and payload.drilldown.selected is True
    assert str(payload.drilldown.guidance.surface) == "tui"
    assert "mode: report-only" in summary_text
    assert "Read-only operational readiness and guidance." in operational_text



def test_build_tui_review_payload_preserves_review_surface_contract_facts(tmp_path: Path) -> None:
    repo_source, scan_result, report_result = _review_inputs(tmp_path)
    payload = build_tui_review_payload(repo_source=repo_source, scan_result=scan_result, report_result=report_result)

    selected_row = payload.selection.selected_rows[0]
    excluded_row = payload.selection.excluded_rows[0]
    excluded_panel_text = _render_text(render_candidate_panel(payload, excluded_row.candidate_id))
    selected_panel_text = _render_text(render_candidate_panel(payload, selected_row.candidate_id))

    assert [row.candidate_id for row in payload.candidate_rows] == [selected_row.candidate_id, excluded_row.candidate_id]
    assert [option.value for option in payload.filters.languages] == ["python", "typescript"]
    assert [option.value for option in payload.filters.apply_modes] == ["auto", "report_only"]
    assert [option.value for option in payload.filters.statuses] == ["excluded", "selected"]
    assert excluded_row.excluded is True
    assert excluded_row.exclusion_reason == "requires boundary review"
    assert payload.drilldown is not None and payload.drilldown.candidate.id == selected_row.candidate_id
    assert "optimizer source" in selected_panel_text
    assert "Authoritative drilldown payload" in selected_panel_text
    assert "exclusion reason" in excluded_panel_text
    assert "requires boundary review" in excluded_panel_text



def test_doctor_and_tui_share_authoritative_review_counts(tmp_path: Path) -> None:
    repo_source, scan_result, report_result = _review_inputs(tmp_path)
    doctor_report = build_doctor_report(repo_source=repo_source, scan_result=scan_result, report_result=report_result)
    tui_payload = build_tui_review_payload(repo_source=repo_source, scan_result=scan_result, report_result=report_result)

    assert doctor_report.source == tui_payload.source
    assert doctor_report.repo == tui_payload.repo
    assert doctor_report.facts.candidate_count == len(tui_payload.candidate_rows)
    assert doctor_report.facts.selected_count == len(tui_payload.selection.selected_rows)
    assert doctor_report.facts.excluded_count == len(tui_payload.selection.excluded_rows)
    assert doctor_report.facts.optimizer_selection_source == tui_payload.selection.optimizer_selection_source
    assert [item.key for item in doctor_report.readiness_items] == [item.key for item in tui_payload.operational.readiness_items]



def test_doctor_surface_ignores_tui_install_blocker_while_tui_requires_it(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    repo_source, scan_result, report_result = _review_inputs(tmp_path)

    monkeypatch.setattr("refactorq.core.tui.builders.importlib.util.find_spec", lambda name: None)
    monkeypatch.setattr("refactorq.core.tui.builders.shutil.which", lambda name: f"/bin/{name}")
    monkeypatch.setattr("refactorq.core.tui.builders.inspect_git_workspace", lambda root: type("GitState", (), {"available": True, "clean": True, "reason": None})())
    monkeypatch.setattr("refactorq.core.tui.builders.CodexGuardedApplier.is_available", lambda self: True)

    doctor_report = build_doctor_report(repo_source=repo_source, scan_result=scan_result, report_result=report_result)
    tui_payload = build_tui_review_payload(repo_source=repo_source, scan_result=scan_result, report_result=report_result)

    assert any(item.key == "tui_install" and item.status == "unavailable" for item in doctor_report.readiness_items)
    assert doctor_report.guidance.command != "install_tui"
    assert doctor_report.guidance.blocking is False
    assert tui_payload.operational.guidance.command == "install_tui"
    assert tui_payload.operational.guidance.blocking is True



def test_create_tui_app_reports_optional_textual_dependency(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    repo_source, scan_result, report_result = _review_inputs(tmp_path)
    payload = build_tui_review_payload(repo_source=repo_source, scan_result=scan_result, report_result=report_result)
    missing_textual = ModuleNotFoundError("No module named 'textual'")
    missing_textual.name = "textual"

    monkeypatch.setattr(tui_app_module, "_TEXTUAL_IMPORT_ERROR", missing_textual)

    with raises(RuntimeError, match="optional 'textual' package"):
        tui_app_module.create_tui_app(payload)



def test_render_doctor_report_surfaces_shared_guidance_facts(tmp_path: Path) -> None:
    repo_source, scan_result, report_result = _review_inputs(tmp_path)
    report = build_doctor_report(repo_source=repo_source, scan_result=scan_result, report_result=report_result)
    console = Console(record=True, width=120)

    render_doctor_report(report, console=console)
    text = console.export_text()

    assert "RefactorQ Doctor:" in text
    assert "selected candidates" in text
    assert "excluded candidates" in text
    assert "Operational readiness and report-mode guidance." in text
