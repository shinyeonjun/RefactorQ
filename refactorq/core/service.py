from __future__ import annotations

from pathlib import Path

from typing import TYPE_CHECKING


from pydantic import BaseModel, Field

from refactorq.adapters.registry import detect_adapters
from refactorq.core.candidate import Candidate
from refactorq.core.execution import ApplyResult, ReportResult, RunResult, apply_plan, report_plan, run_plan
from refactorq.core.planning import PlanMode, PlanResult, build_plan
from refactorq.core.boundary import enrich_boundary_candidates
from refactorq.core.repo import RepoSnapshot, detect_repo
from refactorq.core.repo_source import NormalizedRepoSource, normalize_repo_source
from refactorq.core.verification import VerificationResult
from refactorq.core.verification.service import verify_repo
if TYPE_CHECKING:
    from refactorq.core.tui.models import DoctorReport, TuiReviewPayload



class ScanResult(BaseModel):
    repo: RepoSnapshot
    adapter_names: list[str] = Field(default_factory=list, alias="adapterNames")
    candidates: list[Candidate] = Field(default_factory=list)


def _apply_source_metadata_apply(result: ApplyResult, repo_source: NormalizedRepoSource) -> ApplyResult:
    return result.model_copy(
        update={
            "source_kind": repo_source.kind,
            "working_root": str(repo_source.analysis_root),
        }
    )


def _apply_source_metadata_run(result: RunResult, repo_source: NormalizedRepoSource) -> RunResult:
    return result.model_copy(
        update={
            "source_kind": repo_source.kind,
            "working_root": str(repo_source.analysis_root),
            "apply": result.apply.model_copy(
                update={
                    "source_kind": repo_source.kind,
                    "working_root": str(repo_source.analysis_root),
                }
            ),
        }
    )


def _build_plan_from_scan(scan_result: ScanResult, mode: PlanMode) -> PlanResult:
    return build_plan(
        mode=mode,
        repo=scan_result.repo,
        adapter_names=scan_result.adapter_names,
        candidates=scan_result.candidates,
    )


def _build_report_from_plan(root: Path, plan_result: PlanResult) -> ReportResult:
    return report_plan(root, plan_result)


def _build_report_view(root: Path) -> tuple[ScanResult, PlanResult, ReportResult]:
    scan_result = RefactorQService().scan(root)
    plan_result = _build_plan_from_scan(scan_result, "report")
    report_result = _build_report_from_plan(root, plan_result)
    return scan_result, plan_result, report_result



class RefactorQService:
    def scan_source(self, source: str | Path) -> ScanResult:
        with normalize_repo_source(source) as repo_source:
            return self.scan(repo_source.analysis_root)

    def plan_source(self, source: str | Path, mode: PlanMode) -> PlanResult:
        with normalize_repo_source(source) as repo_source:
            return self.plan(repo_source.analysis_root, mode)

    def scan(self, root: Path) -> ScanResult:
        snapshot = detect_repo(root)
        adapters = detect_adapters(root)
        candidates: list[Candidate] = []
        for adapter in adapters:
            candidates.extend(adapter.scan(root))
        candidates = enrich_boundary_candidates(snapshot, candidates, root)
        return ScanResult(
            repo=snapshot,
            adapterNames=[adapter.name for adapter in adapters],
            candidates=candidates,
        )

    def plan(self, root: Path, mode: PlanMode) -> PlanResult:
        return _build_plan_from_scan(self.scan(root), mode)


    def tui_source(self, source: str | Path) -> TuiReviewPayload:
        from refactorq.core.tui import build_tui_review_payload

        with normalize_repo_source(source) as repo_source:
            scan_result, _, report_result = _build_report_view(repo_source.analysis_root)
            return build_tui_review_payload(
                repo_source=repo_source,
                scan_result=scan_result,
                report_result=report_result,
            )

    def doctor_source(self, source: str | Path) -> DoctorReport:
        from refactorq.core.tui import build_doctor_report

        with normalize_repo_source(source) as repo_source:
            scan_result, _, report_result = _build_report_view(repo_source.analysis_root)
            return build_doctor_report(
                repo_source=repo_source,
                scan_result=scan_result,
                report_result=report_result,
            )


    def apply_source(self, source: str | Path, mode: PlanMode) -> ApplyResult:
        with normalize_repo_source(source, mutable=True) as repo_source:
            return _apply_source_metadata_apply(self.apply(repo_source.analysis_root, mode), repo_source)

    def apply(self, root: Path, mode: PlanMode) -> ApplyResult:
        return apply_plan(root, self.plan(root, mode))

    def verify_source(self, source: str | Path) -> VerificationResult:
        with normalize_repo_source(source) as repo_source:
            return self.verify(repo_source.analysis_root)

    def verify(self, root: Path) -> VerificationResult:
        return verify_repo(root)

    def report_source(self, source: str | Path, mode: PlanMode) -> ReportResult:
        with normalize_repo_source(source) as repo_source:
            return self.report(repo_source.analysis_root, mode)

    def report(self, root: Path, mode: PlanMode) -> ReportResult:
        return _build_report_from_plan(root, self.plan(root, mode))

    def run_source(self, source: str | Path, mode: PlanMode) -> RunResult:
        with normalize_repo_source(source, mutable=True) as repo_source:
            return _apply_source_metadata_run(self.run(repo_source.analysis_root, mode), repo_source)

    def run(self, root: Path, mode: PlanMode) -> RunResult:
        return run_plan(root, self.plan(root, mode))
