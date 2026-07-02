from __future__ import annotations

from collections.abc import Sequence

from refactorq.core.tui.models import (
    GuidanceCommand,
    GuidanceFacts,
    GuidancePriority,
    GuidanceReason,
    GuidanceRecommendation,
    GuidanceStateKey,
    ReadinessItem,
    ReadinessItemKey,
    ReadinessStatus,
    SourceKind,
    Surface,
)


def build_guidance_recommendation(
    *,
    source_kind: SourceKind,
    surface: Surface,
    readiness_items: Sequence[ReadinessItem],
    facts: GuidanceFacts,
) -> GuidanceRecommendation:
    readiness_by_key = {item.key: item for item in readiness_items}

    unavailable_order = [
        ReadinessItemKey.PYTHON_RUNTIME,
        ReadinessItemKey.NODE_RUNTIME,
        ReadinessItemKey.TUI_INSTALL,
        ReadinessItemKey.TS_WORKER,
        ReadinessItemKey.GIT_RUNTIME,
        ReadinessItemKey.GIT_WORKSPACE,
    ]
    degraded_order = [
        ReadinessItemKey.PYTHON_RUNTIME,
        ReadinessItemKey.NODE_RUNTIME,
        ReadinessItemKey.TUI_INSTALL,
        ReadinessItemKey.TS_WORKER,
        ReadinessItemKey.CODEX_GUARDED,
        ReadinessItemKey.GIT_RUNTIME,
        ReadinessItemKey.GIT_WORKSPACE,
    ]
    if surface == Surface.DOCTOR:
        unavailable_order = [key for key in unavailable_order if key != ReadinessItemKey.TUI_INSTALL]
        degraded_order = [key for key in degraded_order if key != ReadinessItemKey.TUI_INSTALL]

    for key in unavailable_order:
        item = readiness_by_key.get(key)
        if item is not None and item.status == ReadinessStatus.UNAVAILABLE:
            return _build_readiness_recommendation(source_kind=source_kind, surface=surface, key=key, status=item.status)

    for key in degraded_order:
        item = readiness_by_key.get(key)
        if item is not None and item.status == ReadinessStatus.DEGRADED:
            return _build_readiness_recommendation(source_kind=source_kind, surface=surface, key=key, status=item.status)

    if facts.selected_candidate_id is not None or facts.selected_count > 0:
        return GuidanceRecommendation(
            surface=surface,
            sourceKind=source_kind,
            stateKey=GuidanceStateKey.SELECTION_READY,
            command=GuidanceCommand.INSPECT_SELECTION,
            reason=GuidanceReason.SELECTED_CANDIDATES_AVAILABLE,
            priority=GuidancePriority.NORMAL,
        )

    if facts.excluded_count > 0 and facts.selected_count == 0:
        return GuidanceRecommendation(
            surface=surface,
            sourceKind=source_kind,
            stateKey=GuidanceStateKey.EXCLUSIONS_READY,
            command=GuidanceCommand.REVIEW_EXCLUDED,
            reason=GuidanceReason.ONLY_EXCLUDED_CANDIDATES_AVAILABLE,
            priority=GuidancePriority.NORMAL,
        )

    if facts.has_active_filters or facts.active_filter_count > 0:
        return GuidanceRecommendation(
            surface=surface,
            sourceKind=source_kind,
            stateKey=GuidanceStateKey.FILTERED_REVIEW,
            command=GuidanceCommand.CONTINUE_REVIEW,
            reason=GuidanceReason.FILTERS_ACTIVE,
            priority=GuidancePriority.LOW,
        )

    if facts.candidate_count == 0:
        command = GuidanceCommand.OPEN_DOCTOR if surface == Surface.TUI else GuidanceCommand.CONTINUE_REVIEW
        return GuidanceRecommendation(
            surface=surface,
            sourceKind=source_kind,
            stateKey=GuidanceStateKey.EMPTY_REVIEW,
            command=command,
            reason=GuidanceReason.NO_CANDIDATES_AVAILABLE,
            priority=GuidancePriority.LOW,
        )

    return GuidanceRecommendation(
        surface=surface,
        sourceKind=source_kind,
        stateKey=GuidanceStateKey.OPERATIONAL_READY,
        command=GuidanceCommand.CONTINUE_REVIEW,
        reason=GuidanceReason.READY_FOR_REVIEW,
        priority=GuidancePriority.LOW,
    )


def _build_readiness_recommendation(
    *,
    source_kind: SourceKind,
    surface: Surface,
    key: ReadinessItemKey,
    status: ReadinessStatus,
) -> GuidanceRecommendation:
    if key == ReadinessItemKey.PYTHON_RUNTIME:
        return _readiness_result(
            source_kind=source_kind,
            surface=surface,
            key=key,
            status=status,
            state_key=GuidanceStateKey.PYTHON_RUNTIME_BLOCKED,
            command=GuidanceCommand.INSTALL_PYTHON_RUNTIME,
        )
    if key == ReadinessItemKey.NODE_RUNTIME:
        return _readiness_result(
            source_kind=source_kind,
            surface=surface,
            key=key,
            status=status,
            state_key=GuidanceStateKey.NODE_RUNTIME_BLOCKED,
            command=GuidanceCommand.INSTALL_NODE_RUNTIME,
        )
    if key == ReadinessItemKey.TUI_INSTALL:
        return _readiness_result(
            source_kind=source_kind,
            surface=surface,
            key=key,
            status=status,
            state_key=GuidanceStateKey.TUI_INSTALL_BLOCKED,
            command=GuidanceCommand.INSTALL_TUI,
        )
    if key == ReadinessItemKey.TS_WORKER:
        return _readiness_result(
            source_kind=source_kind,
            surface=surface,
            key=key,
            status=status,
            state_key=GuidanceStateKey.TS_WORKER_BLOCKED,
            command=GuidanceCommand.BUILD_TS_WORKER,
        )
    if key == ReadinessItemKey.CODEX_GUARDED:
        if surface == Surface.TUI:
            return GuidanceRecommendation(
                surface=surface,
                sourceKind=source_kind,
                stateKey=GuidanceStateKey.CODEX_GUARDED_DEGRADED,
                command=GuidanceCommand.CONTINUE_REVIEW,
                reason=GuidanceReason.CODEX_OPTIONAL_IN_REPORT_MODE,
                priority=GuidancePriority.LOW,
                blocking=False,
                readinessKey=key,
            )
        return _readiness_result(
            source_kind=source_kind,
            surface=surface,
            key=key,
            status=status,
            state_key=GuidanceStateKey.CODEX_GUARDED_DEGRADED,
            command=GuidanceCommand.ENABLE_CODEX_GUARDED,
        )
    if key == ReadinessItemKey.GIT_RUNTIME:
        return _readiness_result(
            source_kind=source_kind,
            surface=surface,
            key=key,
            status=status,
            state_key=GuidanceStateKey.GIT_RUNTIME_BLOCKED,
            command=GuidanceCommand.REPAIR_GIT_RUNTIME,
        )
    if key == ReadinessItemKey.GIT_WORKSPACE:
        command = GuidanceCommand.REPAIR_GIT_WORKSPACE if source_kind == SourceKind.LOCAL else GuidanceCommand.REVIEW_GITHUB_SOURCE
        state_key = GuidanceStateKey.GIT_WORKSPACE_BLOCKED if source_kind == SourceKind.LOCAL else GuidanceStateKey.GITHUB_SOURCE_REVIEW
        return _readiness_result(
            source_kind=source_kind,
            surface=surface,
            key=key,
            status=status,
            state_key=state_key,
            command=command,
        )
    raise ValueError(f"Unsupported readiness key: {key}")


def _readiness_result(
    *,
    source_kind: SourceKind,
    surface: Surface,
    key: ReadinessItemKey,
    status: ReadinessStatus,
    state_key: GuidanceStateKey,
    command: GuidanceCommand,
) -> GuidanceRecommendation:
    return GuidanceRecommendation(
        surface=surface,
        sourceKind=source_kind,
        stateKey=state_key,
        command=command,
        reason=GuidanceReason.READINESS_UNAVAILABLE if status == ReadinessStatus.UNAVAILABLE else GuidanceReason.READINESS_DEGRADED,
        priority=GuidancePriority.BLOCKING if status == ReadinessStatus.UNAVAILABLE else GuidancePriority.HIGH,
        blocking=status == ReadinessStatus.UNAVAILABLE,
        readinessKey=key,
    )


__all__ = ["build_guidance_recommendation"]
