from __future__ import annotations

from pathlib import Path

from refactorq.adapters.python import PythonAdapter
from refactorq.core.candidate import Candidate
from refactorq.core.planning import build_plan
from refactorq.core.repo.models import RepoManifestMap, RepoSnapshot


def _repo_snapshot() -> RepoSnapshot:
    return RepoSnapshot(
        root="/tmp/repo",
        pythonFiles=1,
        typescriptFiles=1,
        javascriptFiles=0,
        manifests=RepoManifestMap(pyproject=True, packageJson=True),
        toolchain=["python", "typescript"],
        languages=["python", "typescript"],
        mixedLanguage=True,
        boundaryArtifacts=[],
    )


def _candidate(
    candidate_id: str,
    *,
    apply_mode_hint: str = "auto",
    semantic_risk: float = 0.1,
    api_risk: float = 0.0,
    runtime_risk: float = 0.0,
    conflict_risk: float = 0.0,
    confidence: float = 0.8,
    maintainability_gain: float = 0.2,
    files: list[str] | None = None,
    symbols: list[str] | None = None,
    start_line: int = 1,
    end_line: int = 1,
    scope: str = "local",
    language: str = "python",
    impact_level: str = "none",
    required_checks: list[str] | None = None,
    cross_language: bool = False,
    provenance_detectors: list[str] | None = None,
    dependencies: list[str] | None = None,
    conflicts: list[str] | None = None,
) -> Candidate:
    return Candidate.model_validate(
        {
            "id": candidate_id,
            "kind": "unused_import",
            "title": f"Candidate {candidate_id}",
            "description": "test candidate",
            "language": language,
            "scope": scope,
            "source": ["static"],
            "files": files or ["src/a.py"],
            "symbols": symbols or [candidate_id],
            "anchorRegions": [
                {"file": (files or ["src/a.py"])[0], "startLine": start_line, "endLine": end_line}
            ],
            "estimatedBenefit": {
                "maintainabilityGain": maintainability_gain,
            },
            "estimatedRisk": {
                "semanticRisk": semantic_risk,
                "apiRisk": api_risk,
                "runtimeRisk": runtime_risk,
                "conflictRisk": conflict_risk,
            },
            "estimatedDiff": {
                "filesTouched": len(files or ["src/a.py"]),
                "linesModified": max(1, end_line - start_line + 1),
            },
            "contextSignals": {},
            "boundaryImpact": {
                "crossLanguage": cross_language,
                "impactLevel": impact_level,
            },
            "confidence": confidence,
            "applyModeHint": apply_mode_hint,
            "requiredChecks": ["parse", "lint"] if required_checks is None else required_checks,
            "dependencies": dependencies or [],
            "conflicts": conflicts or [],
            "provenance": {"detectors": provenance_detectors or ["unit-test-detector"], "evidence": []},
        }
    )


def test_safe_mode_filters_to_low_risk_auto_candidates() -> None:
    candidates = [
        _candidate("safe-auto"),
        _candidate("guarded", apply_mode_hint="guarded"),
        _candidate("boundary-high", impact_level="high"),
        _candidate("cross-language", cross_language=True, impact_level="low", files=["backend/api.py"]),
        _candidate("missing-checks", required_checks=[]),
    ]

    result = build_plan(mode="safe", repo=_repo_snapshot(), adapter_names=["python"], candidates=candidates)

    assert [candidate.id for candidate in result.selected_candidates] == ["safe-auto"]
    assert result.excluded_candidates == []
    assert result.required_checks == ["parse", "lint"]


def test_balanced_mode_surfaces_exclusions_with_reasons() -> None:
    candidates = [
        _candidate("auto-ok"),
        _candidate("guarded-ok", apply_mode_hint="guarded"),
        _candidate("cross-language", cross_language=True, impact_level="low", files=["frontend/client.ts"]),
        _candidate("report-only", apply_mode_hint="report_only"),
        _candidate(
            "bridge-guess",
            language="typescript",
            provenance_detectors=["typescript-bridge-regex"],
        ),
    ]

    result = build_plan(mode="balanced", repo=_repo_snapshot(), adapter_names=["python"], candidates=candidates)

    assert [candidate.id for candidate in result.selected_candidates] == ["auto-ok", "guarded-ok"]
    excluded = {item.candidate.id: item.reason for item in result.excluded_candidates}
    assert (
        excluded["cross-language"]
        == "cross-language candidate retained as report until boundary-aware execution lands"
    )
    assert excluded["report-only"] == "report-only candidate retained as explanatory exclusion"
    assert (
        excluded["bridge-guess"]
        == "unsupported TypeScript bridge guess excluded until worker-backed semantics are available"
    )

def test_report_mode_preserves_deterministic_ranking_order() -> None:
    candidates = [
        _candidate("z-last", files=["src/z.py"], confidence=0.8, maintainability_gain=0.2),
        _candidate("a-first", files=["src/a.py"], confidence=0.8, maintainability_gain=0.2),
        _candidate("lower-risk", semantic_risk=0.05, files=["src/m.py"]),
    ]

    result = build_plan(mode="report", repo=_repo_snapshot(), adapter_names=["python"], candidates=candidates)

    assert [candidate.id for candidate in result.selected_candidates] == [
        "lower-risk",
        "a-first",
        "z-last",
    ]


def test_report_mode_prefers_higher_cycle_reduction_when_risk_matches() -> None:
    maintainability_only = _candidate(
        "maintainability-only",
        files=["src/a.py"],
        confidence=0.8,
        maintainability_gain=0.2,
    )
    cycle_first = Candidate.model_validate(
        {
            **_candidate(
                "cycle-first",
                files=["src/b.py"],
                confidence=0.8,
                maintainability_gain=0.2,
            ).model_dump(by_alias=True),
            "estimatedBenefit": {"cycleReduction": 1.0, "maintainabilityGain": 0.2},
        }
    )

    result = build_plan(
        mode="report",
        repo=_repo_snapshot(),
        adapter_names=["python"],
        candidates=[maintainability_only, cycle_first],
    )

    assert [candidate.id for candidate in result.selected_candidates] == ["cycle-first", "maintainability-only"]



def test_plan_surfaces_conflict_and_dependency_edges() -> None:
    selected = [
        _candidate("shared-region", files=["src/shared.py"], symbols=["alpha"], start_line=10, end_line=20),
        _candidate("same-region", files=["src/shared.py"], symbols=["beta"], start_line=15, end_line=25),
        _candidate(
            "same-symbol",
            files=["src/other.py"],
            symbols=["alpha"],
            start_line=1,
            end_line=2,
            dependencies=["shared-region"],
        ),
        _candidate(
            "module-touch",
            files=["src/shared.py"],
            symbols=["gamma"],
            scope="module",
            start_line=30,
            end_line=40,
            conflicts=["same-symbol"],
        ),
    ]

    result = build_plan(mode="report", repo=_repo_snapshot(), adapter_names=["python"], candidates=selected)

    conflict_edges = {
        (frozenset((edge.from_id, edge.to_id)), edge.reason)
        for edge in result.edges
        if edge.kind == "conflict"
    }
    dependency_edges = {
        (edge.from_id, edge.to_id, edge.reason)
        for edge in result.edges
        if edge.kind == "dependency"
    }
    assert (
        frozenset(("shared-region", "same-region")),
        "overlapping anchor regions in the same file",
    ) in conflict_edges
    assert (
        frozenset(("shared-region", "same-symbol")),
        "same symbol in the same language scope",
    ) in conflict_edges
    assert (
        frozenset(("shared-region", "module-touch")),
        "same file touched with at least one non-local candidate",
    ) in conflict_edges
    assert (
        frozenset(("module-touch", "same-symbol")),
        "explicit conflict declared by candidate",
    ) in conflict_edges
    assert (
        "same-symbol",
        "shared-region",
        "explicit dependency declared by candidate",
    ) in dependency_edges


def test_emitted_candidate_serialization_covers_full_floor(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text("import os\n\nprint('hi')\n", encoding="utf-8")

    candidate = PythonAdapter().scan(tmp_path)[0]
    payload = candidate.model_dump(by_alias=True)

    for key in [
        "id",
        "kind",
        "title",
        "description",
        "language",
        "scope",
        "source",
        "files",
        "symbols",
        "anchorRegions",
        "estimatedBenefit",
        "estimatedRisk",
        "estimatedDiff",
        "contextSignals",
        "boundaryImpact",
        "confidence",
        "applyModeHint",
        "requiredChecks",
        "dependencies",
        "conflicts",
        "provenance",
    ]:
        assert key in payload
