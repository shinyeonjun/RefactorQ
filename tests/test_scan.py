from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from pytest import MonkeyPatch
from typer.testing import CliRunner

from refactorq.cli.main import app
from refactorq.core.repo import RepoManifestMap, RepoSnapshot, detect_repo
from refactorq.core.repo_source import normalize_repo_source
from refactorq.core.service import RefactorQService
import refactorq.core.repo_source.source as repo_source_module
import refactorq.core.service as service_module


runner = CliRunner()


def _repo_snapshot(root: Path) -> RepoSnapshot:
    return RepoSnapshot(
        root=str(root.resolve()),
        pythonFiles=1,
        typescriptFiles=0,
        javascriptFiles=0,
        manifests=RepoManifestMap(),
        toolchain=[],
        languages=["python"],
        mixedLanguage=False,
        boundaryArtifacts=[],
    )


def test_detect_repo_marks_mixed_language_repo() -> None:
    snapshot = detect_repo(Path("."))
    assert snapshot.python_files >= 1
    assert snapshot.typescript_files >= 1
    assert snapshot.mixed_language is True


def test_normalize_repo_source_keeps_local_paths(tmp_path: Path) -> None:
    with normalize_repo_source(tmp_path) as repo_source:
        assert repo_source.kind == "local"
        assert repo_source.analysis_root == tmp_path.resolve()


def test_normalize_repo_source_expands_github_archives(monkeypatch: MonkeyPatch) -> None:
    calls: dict[str, object] = {}

    def fake_resolve(source: str) -> str:
        calls["resolved"] = source
        return "https://github.com/acme/project/archive/refs/heads/main.zip"

    def fake_download(url: str, destination: Path) -> Path:
        calls["download"] = url
        destination.write_text("archive", encoding="utf-8")
        return destination

    def fake_extract(archive_path: Path, destination: Path) -> Path:
        calls["extract"] = archive_path.name
        extracted_root = destination / "project-main"
        extracted_root.mkdir(parents=True)
        return extracted_root

    def fake_cleanup(path: Path) -> None:
        calls["cleanup"] = path

    monkeypatch.setattr(repo_source_module, "resolve_github_archive_url", fake_resolve)
    monkeypatch.setattr(repo_source_module, "_download_file", fake_download)
    monkeypatch.setattr(repo_source_module, "_extract_archive", fake_extract)
    monkeypatch.setattr(repo_source_module, "_cleanup_now_or_defer", fake_cleanup)

    with normalize_repo_source("https://github.com/acme/project") as repo_source:
        assert repo_source.kind == "github"
        assert repo_source.analysis_root.name == "project-main"

    assert calls["resolved"] == "https://github.com/acme/project"
    assert calls["download"] == "https://github.com/acme/project/archive/refs/heads/main.zip"
    assert calls["extract"] == "source.zip"
    assert isinstance(calls["cleanup"], Path)


def test_service_scan_exposes_adapter_names() -> None:
    result = RefactorQService().scan(Path("."))
    assert "python" in result.adapter_names
    assert "typescript" in result.adapter_names


def test_service_scan_source_uses_normalized_repo_source(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    calls: dict[str, object] = {}

    @contextmanager
    def fake_normalize(source: str | Path) -> Iterator[SimpleNamespace]:
        calls["source"] = source
        yield SimpleNamespace(analysis_root=tmp_path)

    monkeypatch.setattr(service_module, "normalize_repo_source", fake_normalize)
    monkeypatch.setattr(service_module, "detect_repo", _repo_snapshot)
    monkeypatch.setattr(service_module, "detect_adapters", lambda root: [])

    result = RefactorQService().scan_source(tmp_path)

    assert calls["source"] == tmp_path
    assert result.repo.root == str(tmp_path.resolve())


def test_scan_command_accepts_github_repo_url(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    calls: dict[str, object] = {}

    @contextmanager
    def fake_normalize(source: str | Path) -> Iterator[SimpleNamespace]:
        calls["source"] = source
        yield SimpleNamespace(analysis_root=tmp_path)

    monkeypatch.setattr(service_module, "normalize_repo_source", fake_normalize)
    monkeypatch.setattr(service_module, "detect_repo", _repo_snapshot)
    monkeypatch.setattr(service_module, "detect_adapters", lambda root: [])

    result = runner.invoke(app, ["scan", "https://github.com/acme/project"])

    assert result.exit_code == 0, result.stdout
    assert calls["source"] == "https://github.com/acme/project"
    assert '"root"' in result.stdout


def test_plan_command_accepts_github_repo_url(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    calls: dict[str, object] = {}

    @contextmanager
    def fake_normalize(source: str | Path) -> Iterator[SimpleNamespace]:
        calls["source"] = source
        yield SimpleNamespace(analysis_root=tmp_path)

    monkeypatch.setattr(service_module, "normalize_repo_source", fake_normalize)
    monkeypatch.setattr(service_module, "detect_repo", _repo_snapshot)
    monkeypatch.setattr(service_module, "detect_adapters", lambda root: [])

    result = runner.invoke(app, ["plan", "https://github.com/acme/project"])

    assert result.exit_code == 0, result.stdout
    assert calls["source"] == "https://github.com/acme/project"
    assert 'selectedCandidates' in result.stdout


def test_deferred_cleanup_runs_on_next_normalization(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    deferred_record = tmp_path / "cleanup.json"
    stale_temp = tmp_path / "stale-temp"
    stale_temp.mkdir()
    local_repo = tmp_path / "repo"
    local_repo.mkdir()

    monkeypatch.setattr(repo_source_module, "_DEFERRED_CLEANUP_RECORD", deferred_record)
    repo_source_module._DEFERRED_CLEANUP.clear()
    repo_source_module._defer_cleanup(stale_temp)

    assert deferred_record.exists()
    assert stale_temp.exists()

    with normalize_repo_source(local_repo) as repo_source:
        assert repo_source.analysis_root == local_repo.resolve()

    assert not stale_temp.exists()
    assert not deferred_record.exists()

def test_service_scan_enriches_boundary_candidates_for_mixed_repo(tmp_path: Path) -> None:
    backend = tmp_path / "backend"
    frontend = tmp_path / "frontend"
    backend.mkdir()
    frontend.mkdir()
    (tmp_path / "openapi.yaml").write_text("openapi: 3.1.0\n", encoding="utf-8")
    (backend / "api.py").write_text("import os\n\nprint('ok')\n", encoding="utf-8")
    (frontend / "client.ts").write_text("function helper() {\n  return 1;\n}\n\nconsole.log('ok');\n", encoding="utf-8")

    result = RefactorQService().scan(tmp_path)

    boundary_review = next(
        candidate for candidate in result.candidates if candidate.id.startswith("boundary-review-openapi-yaml")
    )
    assert boundary_review.apply_mode_hint == "report_only"
    assert boundary_review.boundary_impact.cross_language is True
    assert "openapi" in boundary_review.boundary_impact.boundary_types
    assert boundary_review.files == ["openapi.yaml"]

    enriched_python = next(candidate for candidate in result.candidates if candidate.id.startswith("py-unused-import-backend/api.py"))
    assert enriched_python.boundary_impact.cross_language is True
    assert "http_api" in enriched_python.boundary_impact.boundary_types
    assert "openapi.yaml" in enriched_python.boundary_impact.contract_artifacts
    assert "backend/api.py" in enriched_python.boundary_impact.producer_side
