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
    contract_artifacts: list[str] | None = None,
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
                "contractArtifacts": contract_artifacts or [],
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


def test_balanced_mode_retries_candidates_when_dependency_becomes_satisfied() -> None:
    dependent = Candidate.model_validate(
        {
            **_candidate(
                "dependent-first",
                files=["src/dependent.py"],
                symbols=["shared_helper"],
                confidence=0.95,
                maintainability_gain=0.4,
            ).model_dump(by_alias=True),
            "dependencies": ["dependency-second"],
        }
    )
    dependency = Candidate.model_validate(
        {
            **_candidate(
                "dependency-second",
                files=["src/provider.py"],
                symbols=["provider_helper"],
                confidence=0.6,
                maintainability_gain=0.05,
            ).model_dump(by_alias=True),
        }
    )

    result = build_plan(
        mode="balanced",
        repo=_repo_snapshot(),
        adapter_names=["python"],
        candidates=[dependent, dependency],
    )

    assert [candidate.id for candidate in result.selected_candidates] == ["dependency-second", "dependent-first"]



def test_balanced_mode_surfaces_exclusions_with_reasons() -> None:
    guarded_cross_language = Candidate.model_validate(
        {
            **_candidate(
                "guarded-cross-language-low",
                apply_mode_hint="guarded",
                cross_language=True,
                impact_level="low",
                files=["backend/api.py"],
                symbols=["very_long_function"],
                contract_artifacts=["openapi.yaml"],
            ).model_dump(by_alias=True),
            "kind": "extract_function",
            "scope": "module",
        }
    )
    candidates = [
        _candidate("auto-ok"),
        _candidate("guarded-ok", apply_mode_hint="guarded", files=["src/guarded.py"], symbols=["guarded_symbol"]),
        _candidate("cross-language-low", cross_language=True, impact_level="low", files=["frontend/client.ts"], contract_artifacts=["openapi.yaml"]),
        guarded_cross_language,
        _candidate("cross-language-medium", cross_language=True, impact_level="medium", files=["backend/api.py"]),
        _candidate("report-only", apply_mode_hint="report_only"),
        _candidate(
            "bridge-guess",
            language="typescript",
            provenance_detectors=["typescript-bridge-regex"],
        ),
    ]

    result = build_plan(mode="balanced", repo=_repo_snapshot(), adapter_names=["python"], candidates=candidates)

    assert [candidate.id for candidate in result.selected_candidates] == ["auto-ok", "guarded-ok", "cross-language-low", "guarded-cross-language-low"]
    excluded = {item.candidate.id: item.reason for item in result.excluded_candidates}
    assert (
        excluded["cross-language-medium"]
        == "cross-language candidate requires explicit boundary contract artifacts before balanced execution"
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


def test_safe_mode_enforces_batch_candidate_budget() -> None:
    candidates = [_candidate(f"auto-{index}", files=[f"src/{index}.py"]) for index in range(14)]

    result = build_plan(mode="safe", repo=_repo_snapshot(), adapter_names=["python"], candidates=candidates)

    assert len(result.selected_candidates) == 8
    excluded = {item.candidate.id: item.reason for item in result.excluded_candidates}
    assert excluded["auto-8"] == "safe batch file budget reached"


def test_balanced_mode_enforces_guarded_budget_and_overlap_budget() -> None:
    guarded_candidates = [
        Candidate.model_validate(
            {
                **_candidate(
                    f"guarded-{index}",
                    apply_mode_hint="guarded",
                    files=[f"src/{index}.py"],
                    symbols=[f"symbol_{index}"],
                    start_line=1,
                    end_line=10,
                ).model_dump(by_alias=True),
                "kind": "extract_function",
            }
        )
        for index in range(9)
    ]

    result = build_plan(mode="balanced", repo=_repo_snapshot(), adapter_names=["python"], candidates=guarded_candidates)

    assert len(result.selected_candidates) == 8
    excluded = {item.candidate.id: item.reason for item in result.excluded_candidates}
    assert excluded["guarded-8"] == "balanced guarded candidate budget reached"


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


def test_plan_adds_duplicate_extract_and_cycle_split_dependencies() -> None:
    duplicate = Candidate.model_validate(
        {
            **_candidate(
                "duplicate-same-symbol",
                files=["src/shared.py"],
                symbols=["helper", "other"],
                scope="module",
                apply_mode_hint="guarded",
            ).model_dump(by_alias=True),
            "kind": "duplicate_logic",
            "anchorRegions": [
                {"file": "src/shared.py", "startLine": 5, "endLine": 8},
                {"file": "src/shared.py", "startLine": 20, "endLine": 23},
            ],
        }
    )
    extract = Candidate.model_validate(
        {
            **_candidate(
                "extract-same-symbol",
                files=["src/shared.py"],
                symbols=["helper"],
                start_line=5,
                end_line=30,
                apply_mode_hint="guarded",
            ).model_dump(by_alias=True),
            "kind": "extract_function",
        }
    )
    split = Candidate.model_validate(
        {
            **_candidate(
                "split-module",
                files=["src/shared.py"],
                scope="module",
                apply_mode_hint="report_only",
            ).model_dump(by_alias=True),
            "kind": "split_large_module",
            "anchorRegions": [],
        }
    )
    cycle = Candidate.model_validate(
        {
            **_candidate(
                "reduce-cycle",
                files=["src/shared.py", "src/other.py"],
                scope="package",
                apply_mode_hint="report_only",
                symbols=["pkg.shared", "pkg.other"],
            ).model_dump(by_alias=True),
            "kind": "reduce_cycle",
            "anchorRegions": [],
        }
    )

    result = build_plan(
        mode="report",
        repo=_repo_snapshot(),
        adapter_names=["python"],
        candidates=[duplicate, extract, split, cycle],
    )

    dependency_edges = {
        (edge.from_id, edge.to_id, edge.reason)
        for edge in result.edges
        if edge.kind == "dependency"
    }
    assert (
        "duplicate-same-symbol",
        "extract-same-symbol",
        "extract function before duplicate consolidation in the same file",
    ) in dependency_edges
    assert (
        "split-module",
        "reduce-cycle",
        "reduce cycle before splitting the related module",
    ) in dependency_edges


def test_plan_adds_synergy_edges_for_related_structural_candidates() -> None:
    duplicate = Candidate.model_validate(
        {
            **_candidate(
                "duplicate-wrapper",
                files=["src/shared.py"],
                symbols=["wrapper", "second"],
                apply_mode_hint="guarded",
                scope="module",
            ).model_dump(by_alias=True),
            "kind": "duplicate_logic",
            "anchorRegions": [
                {"file": "src/shared.py", "startLine": 5, "endLine": 8},
                {"file": "src/shared.py", "startLine": 20, "endLine": 23},
            ],
        }
    )
    extract = Candidate.model_validate(
        {
            **_candidate(
                "extract-wrapper",
                files=["src/shared.py"],
                symbols=["wrapper"],
                start_line=5,
                end_line=30,
                apply_mode_hint="guarded",
            ).model_dump(by_alias=True),
            "kind": "extract_function",
        }
    )
    remove = Candidate.model_validate(
        {
            **_candidate(
                "remove-wrapper",
                files=["src/shared.py"],
                symbols=["_wrapper"],
                start_line=32,
                end_line=35,
                apply_mode_hint="guarded",
                scope="module",
            ).model_dump(by_alias=True),
            "kind": "remove_abstraction",
        }
    )
    split = Candidate.model_validate(
        {
            **_candidate(
                "split-shared",
                files=["src/shared.py"],
                apply_mode_hint="report_only",
                scope="module",
            ).model_dump(by_alias=True),
            "kind": "split_large_module",
            "anchorRegions": [],
        }
    )
    cycle = Candidate.model_validate(
        {
            **_candidate(
                "cycle-shared",
                files=["src/shared.py", "src/other.py"],
                symbols=["pkg.shared", "pkg.other"],
                apply_mode_hint="report_only",
                scope="package",
            ).model_dump(by_alias=True),
            "kind": "reduce_cycle",
            "anchorRegions": [],
        }
    )

    result = build_plan(
        mode="report",
        repo=_repo_snapshot(),
        adapter_names=["python"],
        candidates=[duplicate, extract, remove, split, cycle],
    )

    synergy_edges = {
        (frozenset((edge.from_id, edge.to_id)), edge.reason)
        for edge in result.edges
        if edge.kind == "synergy"
    }
    assert (
        frozenset(("duplicate-wrapper", "extract-wrapper")),
        "duplicate consolidation and extraction reinforce the same file refactor",
    ) in synergy_edges
    assert (
        frozenset(("duplicate-wrapper", "remove-wrapper")),
        "duplicate cleanup pairs with removing thin wrappers in the same file",
    ) in synergy_edges
    assert (
        frozenset(("split-shared", "cycle-shared")),
        "cycle reduction and module splitting reinforce the same structural cleanup",
    ) in synergy_edges


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
