from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from refactorq.adapters.registry import detect_adapters
from refactorq.core.candidate import Candidate
from refactorq.core.execution import ApplyResult, ReportResult, RunResult, apply_plan, report_plan, run_plan
from refactorq.core.planning import PlanMode, PlanResult, build_plan
from refactorq.core.boundary import enrich_boundary_candidates
from refactorq.core.repo import RepoSnapshot, detect_repo
from refactorq.core.repo_source import normalize_repo_source
from refactorq.core.verification import VerificationResult
from refactorq.core.verification.service import verify_repo


class ScanResult(BaseModel):
    repo: RepoSnapshot
    adapter_names: list[str] = Field(default_factory=list, alias="adapterNames")
    candidates: list[Candidate] = Field(default_factory=list)


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
        candidates = enrich_boundary_candidates(snapshot, candidates)
        return ScanResult(
            repo=snapshot,
            adapterNames=[adapter.name for adapter in adapters],
            candidates=candidates,
        )

    def plan(self, root: Path, mode: PlanMode) -> PlanResult:
        scan_result = self.scan(root)
        return build_plan(
            mode=mode,
            repo=scan_result.repo,
            adapter_names=scan_result.adapter_names,
            candidates=scan_result.candidates,
        )

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
        return report_plan(root, self.plan(root, mode))

    def run(self, root: Path, mode: PlanMode) -> RunResult:
        return run_plan(root, self.plan(root, mode))
