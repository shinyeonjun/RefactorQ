from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from refactorq.adapters.registry import detect_adapters
from refactorq.core.candidate import Candidate
from refactorq.core.planning import PlanMode, PlanResult, build_plan
from refactorq.core.repo import RepoSnapshot, detect_repo
from refactorq.core.repo_source import normalize_repo_source


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
