from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from refactorq.core.candidate import Candidate
from refactorq.core.candidate.models import ApplyMode, ImpactLevel, Kind, Language, Scope
from refactorq.core.planning.models import SelectionSource
from refactorq.core.repo import RepoSnapshot


class TuiContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, use_enum_values=True)


class SourceKind(StrEnum):
    LOCAL = "local"
    GITHUB = "github"
    GITHUB_CLONE = "github_clone"


class Surface(StrEnum):
    TUI = "tui"
    DOCTOR = "doctor"


class ReadinessItemKey(StrEnum):
    PYTHON_RUNTIME = "python_runtime"
    NODE_RUNTIME = "node_runtime"
    TUI_INSTALL = "tui_install"
    TS_WORKER = "ts_worker"
    CODEX_GUARDED = "codex_guarded"
    GIT_RUNTIME = "git_runtime"
    GIT_WORKSPACE = "git_workspace"


class ReadinessStatus(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    NOT_APPLICABLE = "not_applicable"


class ProbeDepth(StrEnum):
    EXECUTABLE_PRESENCE = "executable_presence"
    IMPORT_AVAILABILITY = "import_availability"
    REPO_OPERATIONAL_READINESS = "repo_operational_readiness"


class GuidanceCommand(StrEnum):
    CONTINUE_REVIEW = "continue_review"
    OPEN_DOCTOR = "open_doctor"
    REVIEW_EXCLUDED = "review_excluded"
    INSPECT_SELECTION = "inspect_selection"
    INSTALL_PYTHON_RUNTIME = "install_python_runtime"
    INSTALL_NODE_RUNTIME = "install_node_runtime"
    INSTALL_TUI = "install_tui"
    BUILD_TS_WORKER = "build_ts_worker"
    ENABLE_CODEX_GUARDED = "enable_codex_guarded"
    REPAIR_GIT_RUNTIME = "repair_git_runtime"
    REPAIR_GIT_WORKSPACE = "repair_git_workspace"
    REVIEW_GITHUB_SOURCE = "review_github_source"


class GuidanceReason(StrEnum):
    READY_FOR_REVIEW = "ready_for_review"
    FILTERS_ACTIVE = "filters_active"
    SELECTED_CANDIDATES_AVAILABLE = "selected_candidates_available"
    ONLY_EXCLUDED_CANDIDATES_AVAILABLE = "only_excluded_candidates_available"
    NO_CANDIDATES_AVAILABLE = "no_candidates_available"
    READINESS_UNAVAILABLE = "readiness_unavailable"
    READINESS_DEGRADED = "readiness_degraded"
    CODEX_OPTIONAL_IN_REPORT_MODE = "codex_optional_in_report_mode"


class GuidancePriority(StrEnum):
    BLOCKING = "blocking"
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


class GuidanceStateKey(StrEnum):
    OPERATIONAL_READY = "operational_ready"
    FILTERED_REVIEW = "filtered_review"
    SELECTION_READY = "selection_ready"
    EXCLUSIONS_READY = "exclusions_ready"
    EMPTY_REVIEW = "empty_review"
    PYTHON_RUNTIME_BLOCKED = "python_runtime_blocked"
    NODE_RUNTIME_BLOCKED = "node_runtime_blocked"
    TUI_INSTALL_BLOCKED = "tui_install_blocked"
    TS_WORKER_BLOCKED = "ts_worker_blocked"
    CODEX_GUARDED_DEGRADED = "codex_guarded_degraded"
    GIT_RUNTIME_BLOCKED = "git_runtime_blocked"
    GIT_WORKSPACE_BLOCKED = "git_workspace_blocked"
    GITHUB_SOURCE_REVIEW = "github_source_review"


class ReadinessItem(TuiContractModel):
    key: ReadinessItemKey
    status: ReadinessStatus
    reason: str | None = None
    probe_depth: ProbeDepth = Field(alias="probeDepth")
    evidence: tuple[str, ...] = ()


class GuidanceRecommendation(TuiContractModel):
    surface: Surface
    source_kind: SourceKind = Field(alias="sourceKind")
    state_key: GuidanceStateKey = Field(alias="stateKey")
    command: GuidanceCommand
    reason: GuidanceReason
    priority: GuidancePriority
    blocking: bool = False
    readiness_key: ReadinessItemKey | None = Field(default=None, alias="readinessKey")


class GuidanceFacts(TuiContractModel):
    candidate_count: int = Field(default=0, alias="candidateCount")
    selected_count: int = Field(default=0, alias="selectedCount")
    excluded_count: int = Field(default=0, alias="excludedCount")
    has_active_filters: bool = Field(default=False, alias="hasActiveFilters")
    active_filter_count: int = Field(default=0, alias="activeFilterCount")
    selected_candidate_id: str | None = Field(default=None, alias="selectedCandidateId")
    optimizer_selection_source: SelectionSource | None = Field(default=None, alias="optimizerSelectionSource")
    report_mode_only: bool = Field(default=True, alias="reportModeOnly")


class TuiSourceMetadata(TuiContractModel):
    source: str
    source_kind: SourceKind = Field(alias="sourceKind")
    working_root: str | None = Field(default=None, alias="workingRoot")
    repo_root: str | None = Field(default=None, alias="repoRoot")
    mutable: bool = False
    preserved: bool = False


class TuiCandidateRow(TuiContractModel):
    candidate_id: str = Field(alias="candidateId")
    title: str
    kind: Kind
    language: Language
    scope: Scope
    apply_mode_hint: ApplyMode = Field(alias="applyModeHint")
    confidence: float
    files: tuple[str, ...] = ()
    selected: bool = False
    excluded: bool = False
    exclusion_reason: str | None = Field(default=None, alias="exclusionReason")
    required_checks: tuple[str, ...] = Field(default=(), alias="requiredChecks")
    proof_ids: tuple[str, ...] = Field(default=(), alias="proofIds")
    boundary_impact_level: ImpactLevel = Field(default="none", alias="boundaryImpactLevel")


class TuiFilterOption(TuiContractModel):
    value: str
    label: str
    count: int = 0
    selected: bool = False


class TuiFilterValues(TuiContractModel):
    query: str = ""
    languages: tuple[TuiFilterOption, ...] = ()
    scopes: tuple[TuiFilterOption, ...] = ()
    kinds: tuple[TuiFilterOption, ...] = ()
    apply_modes: tuple[TuiFilterOption, ...] = Field(default=(), alias="applyModes")
    statuses: tuple[TuiFilterOption, ...] = ()


class TuiSelectionPartition(TuiContractModel):
    optimizer_selection_source: SelectionSource | None = Field(default=None, alias="optimizerSelectionSource")
    selected_rows: tuple[TuiCandidateRow, ...] = Field(default=(), alias="selectedRows")
    excluded_rows: tuple[TuiCandidateRow, ...] = Field(default=(), alias="excludedRows")


class TuiCandidateDrilldown(TuiContractModel):
    candidate: Candidate
    selected: bool = False
    exclusion_reason: str | None = Field(default=None, alias="exclusionReason")
    readiness_items: tuple[ReadinessItem, ...] = Field(default=(), alias="readinessItems")
    guidance: GuidanceRecommendation


class TuiOperationalStatus(TuiContractModel):
    surface: Literal[Surface.TUI] = Surface.TUI
    readiness_items: tuple[ReadinessItem, ...] = Field(default=(), alias="readinessItems")
    guidance: GuidanceRecommendation


class TuiReviewPayload(TuiContractModel):
    surface: Literal[Surface.TUI] = Surface.TUI
    repo: RepoSnapshot
    source: TuiSourceMetadata
    candidate_rows: tuple[TuiCandidateRow, ...] = Field(default=(), alias="candidateRows")
    filters: TuiFilterValues = Field(default_factory=TuiFilterValues)
    selection: TuiSelectionPartition
    drilldown: TuiCandidateDrilldown | None = None
    operational: TuiOperationalStatus


class DoctorReport(TuiContractModel):
    surface: Literal[Surface.DOCTOR] = Surface.DOCTOR
    repo: RepoSnapshot | None = None
    source: TuiSourceMetadata
    readiness_items: tuple[ReadinessItem, ...] = Field(default=(), alias="readinessItems")
    guidance: GuidanceRecommendation
    facts: GuidanceFacts = Field(default_factory=GuidanceFacts)


__all__ = [
    "DoctorReport",
    "GuidanceCommand",
    "GuidanceFacts",
    "GuidancePriority",
    "GuidanceReason",
    "GuidanceRecommendation",
    "GuidanceStateKey",
    "ProbeDepth",
    "ReadinessItem",
    "ReadinessItemKey",
    "ReadinessStatus",
    "SourceKind",
    "Surface",
    "TuiCandidateDrilldown",
    "TuiCandidateRow",
    "TuiContractModel",
    "TuiFilterOption",
    "TuiFilterValues",
    "TuiOperationalStatus",
    "TuiReviewPayload",
    "TuiSelectionPartition",
    "TuiSourceMetadata",
]
