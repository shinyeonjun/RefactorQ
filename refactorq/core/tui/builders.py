from __future__ import annotations

import importlib.util
import shutil
import sys
from functools import lru_cache
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from refactorq.agents.codex import CodexGuardedApplier
from refactorq.core.git_execution import inspect_git_workspace
from refactorq.core.repo_source import NormalizedRepoSource

if TYPE_CHECKING:
    from refactorq.core.tui.models import (
        DoctorReport,
        GuidanceFacts,
        GuidanceRecommendation,
        ReadinessItem,
        TuiCandidateRow,
        TuiReviewPayload,
        TuiSourceMetadata,
    )
    from refactorq.core.execution import ReportResult
    from refactorq.core.service import ScanResult


@lru_cache(maxsize=1)
def _contracts() -> dict[str, Any]:
    models = import_module("refactorq.core.tui.models")
    guidance = import_module("refactorq.core.tui.guidance")
    return {
        "DoctorReport": getattr(models, "DoctorReport"),
        "GuidanceFacts": getattr(models, "GuidanceFacts"),
        "ReadinessItem": getattr(models, "ReadinessItem"),
        "TuiCandidateDrilldown": getattr(models, "TuiCandidateDrilldown"),
        "TuiCandidateRow": getattr(models, "TuiCandidateRow"),
        "TuiFilterOption": getattr(models, "TuiFilterOption"),
        "TuiFilterValues": getattr(models, "TuiFilterValues"),
        "TuiOperationalStatus": getattr(models, "TuiOperationalStatus"),
        "TuiReviewPayload": getattr(models, "TuiReviewPayload"),
        "TuiSelectionPartition": getattr(models, "TuiSelectionPartition"),
        "TuiSourceMetadata": getattr(models, "TuiSourceMetadata"),
        "build_guidance_recommendation": getattr(guidance, "build_guidance_recommendation"),
    }


def _model(name: str, payload: Any) -> Any:
    model_type = cast(Any, _contracts()[name])
    return model_type.model_validate(payload)


def _working_root(repo_source: NormalizedRepoSource) -> str | None:
    if repo_source.kind == "local" or repo_source.mutable:
        return str(repo_source.analysis_root)
    return None



def _source_metadata(repo_source: NormalizedRepoSource, repo_root: str) -> TuiSourceMetadata:
    return cast(
        "TuiSourceMetadata",
        _model(
            "TuiSourceMetadata",
            {
                "source": repo_source.original,
                "sourceKind": repo_source.kind,
                "workingRoot": _working_root(repo_source),
                "repoRoot": repo_root,
                "mutable": repo_source.mutable,
                "preserved": repo_source.preserved,
            },
        ),
    )


def _base_context(repo_source: NormalizedRepoSource, scan_result: "ScanResult", report_result: "ReportResult") -> dict[str, Any]:
    repo = scan_result.repo
    planned_candidates = [
        *report_result.plan.selected_candidates,
        *[excluded.candidate for excluded in report_result.plan.excluded_candidates],
    ]
    return {
        "repoRoot": repo.root,
        "workingRoot": _working_root(repo_source),
        "sourceKind": repo_source.kind,
        "candidateCount": len(scan_result.candidates),
        "selectedCount": report_result.plan.selected_count,
        "excludedCount": report_result.plan.excluded_count,
        "optimizerSelectionSource": report_result.plan.selection_source,
        "hasPythonFiles": repo.python_files > 0,
        "hasTypeScriptFiles": (repo.typescript_files + repo.javascript_files) > 0,
        "hasNodePackageManifest": repo.manifests.package_json,
        "hasPyprojectManifest": repo.manifests.pyproject,
        "codexRequired": any(candidate.apply_mode_hint == "guarded" for candidate in planned_candidates),
        "tsWorkerRequired": "typescript" in scan_result.adapter_names,
    }


def _guidance_facts_payload(base_context: dict[str, Any], *, selected_candidate_id: str | None = None) -> dict[str, Any]:
    return {
        "candidateCount": base_context["candidateCount"],
        "selectedCount": base_context["selectedCount"],
        "excludedCount": base_context["excludedCount"],
        "hasActiveFilters": False,
        "activeFilterCount": 0,
        "selectedCandidateId": selected_candidate_id,
        "optimizerSelectionSource": base_context["optimizerSelectionSource"],
        "reportModeOnly": True,
    }



def _readiness_item(*, key: str, status: str, reason: str, probe_depth: str, evidence: list[str]) -> ReadinessItem:
    return cast(
        "ReadinessItem",
        _model(
            "ReadinessItem",
            {
                "key": key,
                "status": status,
                "reason": reason,
                "probeDepth": probe_depth,
                "evidence": evidence,
            },
        ),
    )


def _runtime_readiness_items(base_context: dict[str, Any]) -> list[Any]:
    python_relevant = bool(base_context["hasPythonFiles"] or base_context["hasPyprojectManifest"])
    node_relevant = bool(base_context["hasTypeScriptFiles"] or base_context["hasNodePackageManifest"])
    python_executable = sys.executable or "python"
    node_executable = shutil.which("node")
    git_executable = shutil.which("git")
    textual_available = importlib.util.find_spec("textual") is not None
    codex_available = CodexGuardedApplier().is_available()
    git_state = inspect_git_workspace(Path(cast(str, base_context["repoRoot"])))
    ts_worker_root = Path(__file__).resolve().parents[3] / "workers" / "ts-adapter"
    ts_worker_entry = ts_worker_root / "src" / "index.ts"
    ts_worker_dist = ts_worker_root / "dist" / "index.js"
    ts_worker_present = ts_worker_entry.exists() or ts_worker_dist.exists()

    git_workspace_status = "ready" if git_state.available and git_state.clean else "degraded" if git_state.available else "unavailable"
    git_workspace_reason = (
        "git repository is available and clean"
        if git_state.available and git_state.clean
        else "git repository is available but the worktree is not clean"
        if git_state.available
        else git_state.reason or "git workspace is unavailable"
    )

    return [
        _readiness_item(
            key="python_runtime",
            status="ready" if python_relevant else "not_applicable",
            reason=(
                f"python runtime available at {python_executable}"
                if python_relevant
                else "repository does not require python runtime"
            ),
            probe_depth="executable_presence",
            evidence=[python_executable],
        ),
        _readiness_item(
            key="node_runtime",
            status=("ready" if node_executable else "unavailable") if node_relevant else "not_applicable",
            reason=(
                f"node runtime available at {node_executable}"
                if node_relevant and node_executable
                else "node runtime is required but was not found"
                if node_relevant
                else "repository does not require node runtime"
            ),
            probe_depth="executable_presence",
            evidence=[node_executable] if node_executable else [],
        ),
        _readiness_item(
            key="tui_install",
            status="ready" if textual_available else "unavailable",
            reason="textual package is installed" if textual_available else "textual package is not installed",
            probe_depth="import_availability",
            evidence=["textual"] if textual_available else [],
        ),
        _readiness_item(
            key="ts_worker",
            status=(
                "ready"
                if node_executable and ts_worker_present
                else "degraded"
                if node_executable
                else "unavailable"
            )
            if base_context["tsWorkerRequired"]
            else "not_applicable",
            reason=(
                "TypeScript worker bridge is available"
                if base_context["tsWorkerRequired"] and node_executable and ts_worker_present
                else "TypeScript worker files are missing from workers/ts-adapter"
                if base_context["tsWorkerRequired"] and node_executable
                else "TypeScript worker requires node runtime"
                if base_context["tsWorkerRequired"]
                else "repository does not require the TypeScript worker"
            ),
            probe_depth="repo_operational_readiness",
            evidence=[str(path) for path in (ts_worker_entry, ts_worker_dist) if path.exists()],
        ),
        _readiness_item(
            key="codex_guarded",
            status="ready" if codex_available else "unavailable" if base_context["codexRequired"] else "not_applicable",
            reason=(
                "codex cli is available for guarded candidates"
                if base_context["codexRequired"] and codex_available
                else "guarded candidates require the codex cli"
                if base_context["codexRequired"]
                else "report selection does not require guarded execution"
            ),
            probe_depth="executable_presence",
            evidence=["codex"] if codex_available else [],
        ),
        _readiness_item(
            key="git_runtime",
            status="ready" if git_executable else "unavailable",
            reason=f"git runtime available at {git_executable}" if git_executable else "git runtime is not available",
            probe_depth="executable_presence",
            evidence=[git_executable] if git_executable else [],
        ),
        _readiness_item(
            key="git_workspace",
            status=git_workspace_status,
            reason=git_workspace_reason,
            probe_depth="repo_operational_readiness",
            evidence=[cast(str, base_context["repoRoot"])] if base_context["repoRoot"] else [],
        ),
    ]


def _operational_state(*, surface: str, base_context: dict[str, Any], selected_candidate_id: str | None = None) -> tuple[list[Any], Any, Any]:
    readiness_items = _runtime_readiness_items(base_context)
    facts = _model("GuidanceFacts", _guidance_facts_payload(base_context, selected_candidate_id=selected_candidate_id))
    recommendation = _contracts()["build_guidance_recommendation"](
        source_kind=base_context["sourceKind"],
        surface=surface,
        readiness_items=readiness_items,
        facts=facts,
    )
    return readiness_items, facts, recommendation


def _candidate_row_payload(*, candidate: Any, selected: bool, exclusion_reason: str | None) -> dict[str, Any]:
    return {
        "candidateId": candidate.id,
        "title": candidate.title,
        "kind": candidate.kind,
        "language": candidate.language,
        "scope": candidate.scope,
        "applyModeHint": candidate.apply_mode_hint,
        "confidence": candidate.confidence,
        "files": list(candidate.files),
        "selected": selected,
        "excluded": not selected,
        "exclusionReason": exclusion_reason,
        "requiredChecks": list(candidate.required_checks),
        "proofIds": list(candidate.proof_ids),
        "boundaryImpactLevel": candidate.boundary_impact.impact_level,
    }


def _filter_options(values: list[str]) -> list[Any]:
    counts: dict[str, int] = {}
    for value in values:
        if value:
            counts[value] = counts.get(value, 0) + 1
    return [
        _model("TuiFilterOption", {"value": value, "label": value, "count": count})
        for value, count in sorted(counts.items())
    ]



def build_tui_review_payload(*, repo_source: NormalizedRepoSource, scan_result: "ScanResult", report_result: "ReportResult") -> TuiReviewPayload:
    selected_rows_payload = [
        _candidate_row_payload(candidate=candidate, selected=True, exclusion_reason=None)
        for candidate in report_result.plan.selected_candidates
    ]
    excluded_rows_payload = [
        _candidate_row_payload(candidate=excluded.candidate, selected=False, exclusion_reason=excluded.reason)
        for excluded in report_result.plan.excluded_candidates
    ]
    selected_rows = [_model("TuiCandidateRow", payload) for payload in selected_rows_payload]
    excluded_rows = [_model("TuiCandidateRow", payload) for payload in excluded_rows_payload]
    all_rows = [*selected_rows, *excluded_rows]
    base_context = _base_context(repo_source, scan_result, report_result)
    selected_candidate_id = report_result.plan.selected_candidates[0].id if report_result.plan.selected_candidates else None
    readiness_items, _, recommendation = _operational_state(
        surface="tui",
        base_context=base_context,
        selected_candidate_id=selected_candidate_id,
    )
    selection = _model(
        "TuiSelectionPartition",
        {
            "optimizerSelectionSource": report_result.plan.selection_source,
            "selectedRows": selected_rows,
            "excludedRows": excluded_rows,
        },
    )
    combined_payload = [*selected_rows_payload, *excluded_rows_payload]
    filters = _model(
        "TuiFilterValues",
        {
            "languages": _filter_options([payload["language"] for payload in combined_payload]),
            "scopes": _filter_options([payload["scope"] for payload in combined_payload]),
            "kinds": _filter_options([payload["kind"] for payload in combined_payload]),
            "applyModes": _filter_options([payload["applyModeHint"] for payload in combined_payload]),
            "statuses": _filter_options([
                *("selected" for _ in selected_rows_payload),
                *("excluded" for _ in excluded_rows_payload),
            ]),
        },
    )
    drilldown = None
    if report_result.plan.selected_candidates:
        candidate = report_result.plan.selected_candidates[0]
        drilldown = _model(
            "TuiCandidateDrilldown",
            {
                "candidate": candidate,
                "selected": True,
                "exclusionReason": None,
                "readinessItems": readiness_items,
                "guidance": recommendation,
            },
        )
    elif report_result.plan.excluded_candidates:
        excluded = report_result.plan.excluded_candidates[0]
        drilldown = _model(
            "TuiCandidateDrilldown",
            {
                "candidate": excluded.candidate,
                "selected": False,
                "exclusionReason": excluded.reason,
                "readinessItems": readiness_items,
                "guidance": recommendation,
            },
        )
    operational = _model(
        "TuiOperationalStatus",
        {
            "surface": "tui",
            "readinessItems": readiness_items,
            "guidance": recommendation,
        },
    )
    return cast(
        "TuiReviewPayload",
        _model(
            "TuiReviewPayload",
            {
                "surface": "tui",
                "repo": scan_result.repo,
                "source": _source_metadata(repo_source, scan_result.repo.root),
                "candidateRows": all_rows,
                "filters": filters,
                "selection": selection,
                "drilldown": drilldown,
                "operational": operational,
            },
        ),
    )



def build_doctor_report(*, repo_source: NormalizedRepoSource, scan_result: "ScanResult", report_result: "ReportResult") -> DoctorReport:
    base_context = _base_context(repo_source, scan_result, report_result)
    readiness_items, facts, recommendation = _operational_state(surface="doctor", base_context=base_context)
    return cast(
        "DoctorReport",
        _model(
            "DoctorReport",
            {
                "surface": "doctor",
                "repo": scan_result.repo,
                "source": _source_metadata(repo_source, scan_result.repo.root),
                "readinessItems": readiness_items,
                "guidance": recommendation,
                "facts": facts,
            },
        ),
    )


__all__ = ["build_doctor_report", "build_tui_review_payload"]
