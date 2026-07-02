from __future__ import annotations

from pathlib import Path

from refactorq.adapters.python import PythonAdapter
from refactorq.core.candidate import Candidate
from refactorq.core.planning import GreedySelectionBackend, QuboLocalSearchSolver, build_plan
from refactorq.core.planning.service import build_optimizer_problem
from refactorq.core.repo.models import RepoManifestMap, RepoSnapshot


def _repo_snapshot(root: str = "/tmp/repo", *, boundary_artifacts: list[str] | None = None) -> RepoSnapshot:
    return RepoSnapshot(
        root=root,
        pythonFiles=1,
        typescriptFiles=1,
        javascriptFiles=0,
        manifests=RepoManifestMap(pyproject=True, packageJson=True),
        toolchain=["python", "typescript"],
        languages=["python", "typescript"],
        mixedLanguage=True,
        boundaryArtifacts=boundary_artifacts or [],
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
    excluded = {item.candidate.id: item.reason for item in result.excluded_candidates}
    assert excluded == {
        "guarded": "requires guarded or report-only handling",
        "boundary-high": "boundary-changing candidates are excluded in safe mode",
        "cross-language": "cross-language boundary candidates are excluded in safe mode",
        "missing-checks": "candidate is missing required checks",
    }
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



def test_balanced_mode_surfaces_exclusions_with_reasons(tmp_path: Path) -> None:
    backend = tmp_path / "backend"
    frontend = tmp_path / "frontend"
    backend.mkdir()
    frontend.mkdir()
    (backend / "api.py").write_text('ROUTE = "/items"\n', encoding="utf-8")
    (frontend / "client.ts").write_text('export const route = "/items";\n', encoding="utf-8")
    (tmp_path / "openapi.yaml").write_text("openapi: 3.1.0\npaths:\n  /items:\n    get:\n      operationId: listItems\n", encoding="utf-8")

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

    result = build_plan(
        mode="balanced",
        repo=_repo_snapshot(str(tmp_path), boundary_artifacts=["openapi.yaml"]),
        adapter_names=["python"],
        candidates=candidates,
    )

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



def test_report_mode_prioritizes_candidate_value_over_apply_mode() -> None:
    high_value_report = Candidate.model_validate(
        {
            **_candidate(
                "high-value-report",
                apply_mode_hint="report_only",
                maintainability_gain=0.6,
                files=["src/report.py"],
            ).model_dump(by_alias=True),
            "kind": "split_large_module",
            "anchorRegions": [],
            "estimatedBenefit": {
                "complexityReduction": 1.0,
                "duplicationReduction": 1.0,
                "maintainabilityGain": 0.6,
            },
        }
    )
    low_value_auto = _candidate("low-value-auto", maintainability_gain=0.05, files=["src/auto.py"])

    result = build_plan(
        mode="report",
        repo=_repo_snapshot(),
        adapter_names=["python"],
        candidates=[low_value_auto, high_value_report],
    )

    assert [candidate.id for candidate in result.selected_candidates] == ["low-value-auto"]
    assert result.selection_source == "planner_override_of_optimizer"
    assert result.proposal_revalidation.status == "overridden"
    assert result.baseline_comparison is not None
    assert result.baseline_comparison.heuristic_selected_candidate_ids == ["high-value-report", "low-value-auto"]
    assert result.baseline_comparison.optimizer_selected_candidate_ids


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


def test_balanced_mode_excludes_same_symbol_scope_conflicts_from_selected_batch() -> None:
    keep = _candidate(
        "keep-shared-symbol",
        files=["src/shared.py"],
        symbols=["shared_helper"],
        scope="module",
        confidence=0.95,
        maintainability_gain=0.35,
    )
    conflict = _candidate(
        "conflict-shared-symbol",
        files=["src/other.py"],
        symbols=["shared_helper"],
        scope="module",
        confidence=0.6,
        maintainability_gain=0.1,
    )

    result = build_plan(
        mode="balanced",
        repo=_repo_snapshot(),
        adapter_names=["python"],
        candidates=[conflict, keep],
    )

    assert [candidate.id for candidate in result.selected_candidates] == ["keep-shared-symbol"]
    excluded = {item.candidate.id: item.reason for item in result.excluded_candidates}
    assert (
        excluded["conflict-shared-symbol"]
        == "candidate shares the same symbol and scope as already selected batch candidate keep-shared-symbol"
    )


def test_balanced_mode_excludes_same_file_non_local_conflicts_from_selected_batch() -> None:
    keep = _candidate(
        "keep-module-file",
        files=["src/shared.py"],
        symbols=["module_helper"],
        scope="module",
        confidence=0.95,
        maintainability_gain=0.35,
    )
    conflict = _candidate(
        "conflict-module-file",
        files=["src/shared.py"],
        symbols=["other_helper"],
        scope="module",
        start_line=20,
        end_line=25,
        confidence=0.6,
        maintainability_gain=0.1,
    )

    result = build_plan(
        mode="balanced",
        repo=_repo_snapshot(),
        adapter_names=["python"],
        candidates=[conflict, keep],
    )

    assert [candidate.id for candidate in result.selected_candidates] == ["keep-module-file"]
    excluded = {item.candidate.id: item.reason for item in result.excluded_candidates}
    assert (
        excluded["conflict-module-file"]
        == "candidate touches the same file as non-local already selected batch candidate keep-module-file"
    )


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


def test_plan_adds_boundary_review_and_move_symbol_dependencies() -> None:
    boundary_review = Candidate.model_validate(
        {
            **_candidate(
                "boundary-review-openapi-yaml",
                files=["openapi.yaml"],
                scope="architecture",
                apply_mode_hint="report_only",
                language="mixed",
            ).model_dump(by_alias=True),
            "kind": "custom",
            "boundaryImpact": {
                "crossLanguage": True,
                "boundaryTypes": ["openapi", "http_api"],
                "contractArtifacts": ["openapi.yaml"],
                "impactLevel": "high",
            },
        }
    )
    layer_violation = Candidate.model_validate(
        {
            **_candidate(
                "layer-violation",
                files=["frontend/ui.ts", "backend/service.ts"],
                symbols=["service"],
                scope="package",
                apply_mode_hint="report_only",
                language="typescript",
            ).model_dump(by_alias=True),
            "kind": "layer_violation_fix",
        }
    )
    move_symbol = Candidate.model_validate(
        {
            **_candidate(
                "move-symbol",
                files=["frontend/ui.ts", "backend/service.ts"],
                symbols=["service"],
                scope="package",
                apply_mode_hint="report_only",
                language="typescript",
            ).model_dump(by_alias=True),
            "kind": "move_symbol",
            "boundaryImpact": {
                "crossLanguage": True,
                "boundaryTypes": ["openapi", "http_api"],
                "producerSide": ["backend/service.ts"],
                "consumerSide": ["frontend/ui.ts"],
                "contractArtifacts": ["openapi.yaml"],
                "impactLevel": "high",
            },
        }
    )

    result = build_plan(
        mode="report",
        repo=_repo_snapshot(),
        adapter_names=["typescript"],
        candidates=[boundary_review, layer_violation, move_symbol],
    )

    dependency_edges = {
        (edge.from_id, edge.to_id, edge.reason)
        for edge in result.edges
        if edge.kind == "dependency"
    }
    assert (
        "move-symbol",
        "layer-violation",
        "review layer violation before moving the shared boundary symbol",
    ) in dependency_edges
    assert (
        "move-symbol",
        "boundary-review-openapi-yaml",
        "review boundary contract artifact before cross-language execution",
    ) in dependency_edges


def test_balanced_mode_preserves_edges_for_excluded_boundary_review_candidates() -> None:
    boundary_review = Candidate.model_validate(
        {
            **_candidate(
                "boundary-review-openapi-yaml",
                files=["openapi.yaml"],
                scope="architecture",
                apply_mode_hint="report_only",
                language="mixed",
            ).model_dump(by_alias=True),
            "kind": "custom",
            "boundaryImpact": {
                "crossLanguage": True,
                "boundaryTypes": ["openapi", "http_api"],
                "contractArtifacts": ["openapi.yaml"],
                "impactLevel": "high",
            },
        }
    )
    cross_language = Candidate.model_validate(
        {
            **_candidate(
                "cross-language-low",
                cross_language=True,
                impact_level="low",
                files=["backend/api.py"],
                contract_artifacts=["openapi.yaml"],
            ).model_dump(by_alias=True),
            "kind": "extract_function",
            "applyModeHint": "guarded",
            "scope": "module",
            "symbols": ["very_long_function"],
        }
    )

    result = build_plan(
        mode="balanced",
        repo=_repo_snapshot(),
        adapter_names=["python"],
        candidates=[cross_language, boundary_review],
    )

    dependency_edges = {
        (edge.from_id, edge.to_id, edge.reason)
        for edge in result.edges
        if edge.kind == "dependency"
    }
    assert (
        "cross-language-low",
        "boundary-review-openapi-yaml",
        "review boundary contract artifact before cross-language execution",
    ) in dependency_edges


def test_balanced_mode_prefers_lower_verification_burden_when_scores_match() -> None:
    lighter = _candidate("lighter", files=["src/a.py"], required_checks=["parse", "lint"])
    heavier = _candidate("heavier", files=["src/b.py"], required_checks=["parse", "lint", "typecheck", "unit_test"])

    result = build_plan(
        mode="balanced",
        repo=_repo_snapshot(),
        adapter_names=["python"],
        candidates=[heavier, lighter],
    )

    assert [candidate.id for candidate in result.selected_candidates] == ["lighter", "heavier"]


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
    layer_fix = Candidate.model_validate(
        {
            **_candidate(
                "layer-fix",
                files=["src/frontend/ui.py", "src/backend/service.py"],
                symbols=["service"],
                scope="architecture",
                apply_mode_hint="report_only",
                impact_level="high",
            ).model_dump(by_alias=True),
            "kind": "layer_violation_fix",
            "anchorRegions": [{"file": "src/frontend/ui.py", "startLine": 4, "endLine": 4}],
        }
    )
    move_symbol = Candidate.model_validate(
        {
            **_candidate(
                "move-symbol",
                files=["src/frontend/ui.py", "src/backend/service.py"],
                symbols=["service"],
                scope="architecture",
                apply_mode_hint="report_only",
                impact_level="high",
            ).model_dump(by_alias=True),
            "kind": "move_symbol",
            "anchorRegions": [{"file": "src/frontend/ui.py", "startLine": 4, "endLine": 4}],
        }
    )

    result = build_plan(
        mode="report",
        repo=_repo_snapshot(),
        adapter_names=["python"],
        candidates=[duplicate, extract, remove, split, cycle, layer_fix, move_symbol],
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
    assert (
        frozenset(("layer-fix", "move-symbol")),
        "layer boundary review and symbol relocation target the same cross-layer import",
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


def test_build_optimizer_problem_preserves_shared_budget_and_candidate_features() -> None:
    guarded = Candidate.model_validate(
        {
            **_candidate(
                "guarded-proposal",
                apply_mode_hint="guarded",
                files=["src/guarded.py"],
                symbols=["guarded_symbol"],
                semantic_risk=0.45,
            ).model_dump(by_alias=True),
            "kind": "extract_function",
        }
    )
    duplicate = Candidate.model_validate(
        {
            **_candidate(
                "duplicate-proposal",
                files=["src/duplicate.py"],
                symbols=["dup_symbol"],
                maintainability_gain=0.6,
                conflicts=["guarded-proposal"],
            ).model_dump(by_alias=True),
            "kind": "duplicate_logic",
        }
    )

    problem = build_optimizer_problem(
        mode="balanced",
        repo=_repo_snapshot(),
        adapter_names=["python"],
        candidates=[guarded, duplicate],
    )

    assert problem.budget.mode_budget == 24
    assert problem.budget.max_files == 16
    assert [item.candidate.id for item in problem.candidates] == ["guarded-proposal", "duplicate-proposal"]
    by_id = {item.candidate.id: item for item in problem.candidates}
    assert by_id["guarded-proposal"].guarded is True
    assert by_id["guarded-proposal"].high_risk is True
    assert by_id["duplicate-proposal"].conflict_ids == ["guarded-proposal"]



def test_optimizer_backends_share_problem_model_and_emit_distinct_backend_names() -> None:
    first = Candidate.model_validate(
        {
            **_candidate(
                "alpha",
                files=["src/a.py"],
                symbols=["alpha"],
                maintainability_gain=0.35,
                semantic_risk=0.05,
            ).model_dump(by_alias=True),
            "kind": "unused_import",
        }
    )
    second = Candidate.model_validate(
        {
            **_candidate(
                "beta",
                files=["src/b.py"],
                symbols=["beta"],
                maintainability_gain=0.30,
                semantic_risk=0.05,
            ).model_dump(by_alias=True),
            "kind": "unused_import",
        }
    )

    problem = build_optimizer_problem(
        mode="safe",
        repo=_repo_snapshot(),
        adapter_names=["python"],
        candidates=[first, second],
    )

    greedy = GreedySelectionBackend().solve(problem)
    qubo = QuboLocalSearchSolver().solve(problem)

    assert greedy.backend == "greedy"
    assert qubo.backend == "qubo_local_search"
    assert greedy.selected_candidate_ids
    assert qubo.selected_candidate_ids
    assert set(greedy.selected_candidate_ids).issubset({"alpha", "beta"})
    assert set(qubo.selected_candidate_ids).issubset({"alpha", "beta"})
    assert greedy.hard_constraint_status == "satisfied"
    assert qubo.hard_constraint_status == "satisfied"


def test_build_plan_surfaces_accepted_optimizer_selection_source() -> None:
    first = Candidate.model_validate(
        {
            **_candidate(
                "optimizer-alpha",
                files=["src/a.py"],
                symbols=["alpha"],
                maintainability_gain=0.35,
                semantic_risk=0.05,
            ).model_dump(by_alias=True),
            "kind": "unused_import",
        }
    )
    second = Candidate.model_validate(
        {
            **_candidate(
                "optimizer-beta",
                files=["src/b.py"],
                symbols=["beta"],
                maintainability_gain=0.30,
                semantic_risk=0.05,
            ).model_dump(by_alias=True),
            "kind": "unused_import",
        }
    )

    result = build_plan(
        mode="report",
        repo=_repo_snapshot(),
        adapter_names=["python"],
        candidates=[first, second],
    )

    assert result.selection_source == "optimizer_qubo"
    assert result.proposal_revalidation.status == "accepted"
    assert result.solver_proposal is not None
    assert result.solver_proposal.backend == "qubo_local_search"
    assert result.proposal_revalidation.final_selected_candidate_ids == [candidate.id for candidate in result.selected_candidates]

def test_build_plan_rejects_optimizer_candidates_without_boundary_readiness(tmp_path: Path) -> None:
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / "api.py").write_text('ROUTE = "/items"\n', encoding="utf-8")

    candidate = Candidate.model_validate(
        {
            **_candidate(
                "optimizer-boundary",
                apply_mode_hint="guarded",
                cross_language=True,
                impact_level="low",
                files=["backend/api.py"],
                contract_artifacts=["missing-openapi.yaml"],
                required_checks=["parse", "lint", "typecheck", "build", "integration_test"],
            ).model_dump(by_alias=True),
            "kind": "extract_function",
            "scope": "module",
        }
    )

    result = build_plan(
        mode="balanced",
        repo=_repo_snapshot(str(tmp_path), boundary_artifacts=[]),
        adapter_names=["python"],
        candidates=[candidate],
    )

    assert result.selection_source == "optimizer_rejected_no_batch"
    assert result.proposal_revalidation.status == "rejected"
    assert result.selected_candidates == []
    assert result.excluded_candidates[0].candidate.id == "optimizer-boundary"
    assert "boundary contract artifacts are missing from the working tree" in result.excluded_candidates[0].reason
