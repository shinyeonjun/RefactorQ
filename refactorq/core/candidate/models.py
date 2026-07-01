from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Language = Literal["python", "typescript", "javascript", "mixed", "unknown"]
Scope = Literal["local", "module", "package", "architecture", "performance"]
Kind = Literal[
    "unused_import",
    "dead_code",
    "unused_symbol",
    "rename_symbol",
    "extract_function",
    "inline_function",
    "duplicate_logic",
    "split_large_module",
    "move_symbol",
    "reduce_cycle",
    "remove_abstraction",
    "layer_violation_fix",
    "perf_hotspot_refactor",
    "custom",
]
Source = Literal["static", "clone", "graph", "metric", "history", "agent"]
ApplyMode = Literal["auto", "guarded", "report_only"]
VerificationCheck = Literal[
    "parse",
    "lint",
    "typecheck",
    "build",
    "unit_test",
    "integration_test",
    "benchmark",
    "coverage_check",
]
RequestedProofKind = Literal[
    "parse",
    "lint",
    "typecheck",
    "build",
    "unit_test",
    "integration_test",
    "boundary_contract",
    "boundary_integration",
    "manual_review",
]
BoundaryType = Literal[
    "http_api",
    "graphql",
    "json_schema",
    "openapi",
    "config",
    "env",
    "cli",
    "generated_client",
    "db_schema",
    "event_schema",
    "file_format",
    "shared_constants",
]
ImpactLevel = Literal["none", "low", "medium", "high"]


class AnchorRegion(BaseModel):
    file: str
    start_line: int = Field(alias="startLine")
    end_line: int = Field(alias="endLine")


class EstimatedBenefit(BaseModel):
    complexity_reduction: float = Field(default=0.0, alias="complexityReduction")
    duplication_reduction: float = Field(default=0.0, alias="duplicationReduction")
    cycle_reduction: float = Field(default=0.0, alias="cycleReduction")
    maintainability_gain: float = Field(default=0.0, alias="maintainabilityGain")
    perf_gain: float = Field(default=0.0, alias="perfGain")


class EstimatedRisk(BaseModel):
    semantic_risk: float = Field(default=0.0, alias="semanticRisk")
    api_risk: float = Field(default=0.0, alias="apiRisk")
    test_risk: float = Field(default=0.0, alias="testRisk")
    runtime_risk: float = Field(default=0.0, alias="runtimeRisk")
    conflict_risk: float = Field(default=0.0, alias="conflictRisk")


class EstimatedDiff(BaseModel):
    files_touched: int = Field(default=0, alias="filesTouched")
    lines_added: int = Field(default=0, alias="linesAdded")
    lines_deleted: int = Field(default=0, alias="linesDeleted")
    lines_modified: int = Field(default=0, alias="linesModified")


class ContextSignals(BaseModel):
    coverage_ratio: float | None = Field(default=None, alias="coverageRatio")
    hotspot_score: float | None = Field(default=None, alias="hotspotScore")
    churn_score: float | None = Field(default=None, alias="churnScore")
    fan_in: int | None = Field(default=None, alias="fanIn")
    fan_out: int | None = Field(default=None, alias="fanOut")
    public_api_exposure: bool = Field(default=False, alias="publicApiExposure")
    benchmark_available: bool = Field(default=False, alias="benchmarkAvailable")


class BoundaryImpact(BaseModel):
    cross_language: bool = Field(default=False, alias="crossLanguage")
    boundary_types: list[BoundaryType] = Field(default_factory=list, alias="boundaryTypes")
    producer_side: list[str] = Field(default_factory=list, alias="producerSide")
    consumer_side: list[str] = Field(default_factory=list, alias="consumerSide")
    contract_artifacts: list[str] = Field(default_factory=list, alias="contractArtifacts")
    impact_level: ImpactLevel = Field(default="none", alias="impactLevel")


class Provenance(BaseModel):
    detectors: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)


class Candidate(BaseModel):
    id: str
    kind: Kind
    title: str
    description: str
    language: Language = "unknown"
    scope: Scope = "local"
    source: list[Source] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)
    symbols: list[str] = Field(default_factory=list)
    anchor_regions: list[AnchorRegion] = Field(default_factory=list, alias="anchorRegions")
    estimated_benefit: EstimatedBenefit = Field(default_factory=EstimatedBenefit, alias="estimatedBenefit")
    estimated_risk: EstimatedRisk = Field(default_factory=EstimatedRisk, alias="estimatedRisk")
    estimated_diff: EstimatedDiff = Field(default_factory=EstimatedDiff, alias="estimatedDiff")
    context_signals: ContextSignals = Field(default_factory=ContextSignals, alias="contextSignals")
    boundary_impact: BoundaryImpact = Field(default_factory=BoundaryImpact, alias="boundaryImpact")
    confidence: float = 0.0
    apply_mode_hint: ApplyMode = Field(default="report_only", alias="applyModeHint")
    required_checks: list[VerificationCheck] = Field(default_factory=list, alias="requiredChecks")
    proof_ids: list[str] = Field(default_factory=list, alias="proofIds")
    requested_proof_kinds: list[RequestedProofKind] = Field(default_factory=list, alias="requestedProofKinds")
    dependencies: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    provenance: Provenance = Field(default_factory=Provenance)


class CandidateBatch(BaseModel):
    mode: Literal["safe", "balanced", "report"]
    candidates: list[Candidate] = Field(default_factory=list)
