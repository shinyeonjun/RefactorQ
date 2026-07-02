from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from refactorq.core.tui.models import (
    GuidanceRecommendation,
    ReadinessItem,
    TuiCandidateDrilldown,
    TuiCandidateRow,
    TuiFilterOption,
    TuiReviewPayload,
)


@dataclass(slots=True)
class FilterSelection:
    kind: str = "all"
    language: str = "all"
    apply_mode: str = "all"
    status: str = "all"

    @property
    def active_count(self) -> int:
        return sum(value != "all" for value in (self.kind, self.language, self.apply_mode, self.status))


def select_options(options: Iterable[TuiFilterOption], *, all_label: str) -> list[tuple[str, str]]:
    values = [(all_label, "all")]
    values.extend((f"{option.label} ({option.count})", option.value) for option in options)
    return values


def filter_rows(rows: Iterable[TuiCandidateRow], filters: FilterSelection) -> list[TuiCandidateRow]:
    filtered: list[TuiCandidateRow] = []
    for row in rows:
        status = "selected" if row.selected else "excluded"
        if filters.kind != "all" and row.kind != filters.kind:
            continue
        if filters.language != "all" and row.language != filters.language:
            continue
        if filters.apply_mode != "all" and row.apply_mode_hint != filters.apply_mode:
            continue
        if filters.status != "all" and status != filters.status:
            continue
        filtered.append(row)
    return filtered


def resolve_drilldown(payload: TuiReviewPayload, candidate_id: str | None) -> tuple[TuiCandidateRow | None, TuiCandidateDrilldown | None]:
    if candidate_id is None:
        return None, None
    row = next((item for item in payload.candidate_rows if item.candidate_id == candidate_id), None)
    drilldown = payload.drilldown
    if drilldown is not None and drilldown.candidate.id != candidate_id:
        drilldown = None
    return row, drilldown


def render_summary(payload: TuiReviewPayload, filtered_rows: list[TuiCandidateRow], filters: FilterSelection) -> RenderableType:
    selected_count = sum(1 for row in filtered_rows if row.selected)
    excluded_count = sum(1 for row in filtered_rows if row.excluded)
    table = Table.grid(expand=True)
    table.add_column(ratio=1)
    table.add_column(ratio=1)
    table.add_column(ratio=1)
    table.add_column(ratio=1)
    table.add_row(
        f"repo: {payload.repo.root}",
        f"visible: {len(filtered_rows)}/{len(payload.candidate_rows)}",
        f"selected: {selected_count}",
        f"excluded: {excluded_count}",
    )
    table.add_row(
        f"optimizer source: {payload.selection.optimizer_selection_source or 'unknown'}",
        f"source kind: {payload.source.source_kind}",
        f"active filters: {filters.active_count}",
        "mode: report-only",
    )
    return Panel(table, title="Review", border_style="cyan")


def render_guidance(guidance: GuidanceRecommendation) -> Table:
    table = Table.grid(expand=True)
    table.add_column(style="bold cyan", ratio=1)
    table.add_column(ratio=3)
    table.add_row("state", guidance.state_key)
    table.add_row("command", guidance.command)
    table.add_row("reason", guidance.reason)
    table.add_row("priority", guidance.priority)
    table.add_row("blocking", "yes" if guidance.blocking else "no")
    if guidance.readiness_key:
        table.add_row("readiness key", guidance.readiness_key)
    return table


def render_readiness_items(items: Iterable[ReadinessItem]) -> Table:
    table = Table(box=None, expand=True, show_header=True, header_style="bold")
    table.add_column("key", ratio=2)
    table.add_column("status", ratio=1)
    table.add_column("reason", ratio=4)
    for item in items:
        evidence = f" evidence: {', '.join(item.evidence)}" if item.evidence else ""
        table.add_row(item.key, item.status, f"{item.reason or '-'}{evidence}")
    return table


def render_operational_panel(payload: TuiReviewPayload) -> RenderableType:
    return Panel(
        Group(
            Text("Read-only operational readiness and guidance.", style="dim"),
            render_guidance(payload.operational.guidance),
            Text(""),
            render_readiness_items(payload.operational.readiness_items),
        ),
        title="Operational",
        border_style="green",
    )


def render_candidate_panel(payload: TuiReviewPayload, candidate_id: str | None) -> RenderableType:
    row, drilldown = resolve_drilldown(payload, candidate_id)
    if row is None:
        return Panel(Text("Select a candidate row to inspect.", style="dim"), title="Drilldown", border_style="magenta")

    facts = Table.grid(expand=True)
    facts.add_column(style="bold cyan", ratio=1)
    facts.add_column(ratio=3)
    facts.add_row("id", row.candidate_id)
    facts.add_row("title", row.title)
    facts.add_row("kind", row.kind)
    facts.add_row("language", row.language)
    facts.add_row("scope", row.scope)
    facts.add_row("apply mode", row.apply_mode_hint)
    facts.add_row("selected", "yes" if row.selected else "no")
    facts.add_row("excluded", "yes" if row.excluded else "no")
    facts.add_row("confidence", f"{row.confidence:.2f}")
    facts.add_row("boundary impact", row.boundary_impact_level)
    facts.add_row("optimizer source", payload.selection.optimizer_selection_source or "unknown")
    facts.add_row("exclusion reason", row.exclusion_reason or "-")
    facts.add_row("required checks", ", ".join(row.required_checks) or "-")
    facts.add_row("proof ids", ", ".join(row.proof_ids) or "-")
    facts.add_row("files", "\n".join(row.files) or "-")

    sections: list[RenderableType] = [facts]
    if drilldown is not None:
        candidate = drilldown.candidate
        extra = Table.grid(expand=True)
        extra.add_column(style="bold cyan", ratio=1)
        extra.add_column(ratio=3)
        extra.add_row("description", candidate.description)
        extra.add_row("symbols", ", ".join(candidate.symbols) or "-")
        extra.add_row("dependencies", ", ".join(candidate.dependencies) or "-")
        extra.add_row("conflicts", ", ".join(candidate.conflicts) or "-")
        extra.add_row("sources", ", ".join(candidate.source) or "-")
        extra.add_row("provenance", ", ".join(candidate.provenance.detectors) or "-")
        extra.add_row("provenance evidence", "\n".join(candidate.provenance.evidence) or "-")
        extra.add_row("anchor regions", "\n".join(f"{anchor.file}:{anchor.start_line}-{anchor.end_line}" for anchor in candidate.anchor_regions) or "-")
        extra.add_row(
            "estimated diff",
            f"files={candidate.estimated_diff.files_touched}, +{candidate.estimated_diff.lines_added}, -{candidate.estimated_diff.lines_deleted}, ~{candidate.estimated_diff.lines_modified}",
        )
        extra.add_row(
            "estimated risk",
            f"semantic={candidate.estimated_risk.semantic_risk:.2f}, api={candidate.estimated_risk.api_risk:.2f}, test={candidate.estimated_risk.test_risk:.2f}, runtime={candidate.estimated_risk.runtime_risk:.2f}, conflict={candidate.estimated_risk.conflict_risk:.2f}",
        )
        extra.add_row(
            "estimated benefit",
            f"complexity={candidate.estimated_benefit.complexity_reduction:.2f}, duplication={candidate.estimated_benefit.duplication_reduction:.2f}, cycle={candidate.estimated_benefit.cycle_reduction:.2f}, maintainability={candidate.estimated_benefit.maintainability_gain:.2f}, perf={candidate.estimated_benefit.perf_gain:.2f}",
        )
        extra.add_row(
            "boundary types",
            ", ".join(candidate.boundary_impact.boundary_types) or "-",
        )
        extra.add_row(
            "boundary producers",
            ", ".join(candidate.boundary_impact.producer_side) or "-",
        )
        extra.add_row(
            "boundary consumers",
            ", ".join(candidate.boundary_impact.consumer_side) or "-",
        )
        extra.add_row(
            "contract artifacts",
            "\n".join(candidate.boundary_impact.contract_artifacts) or "-",
        )
        sections.extend(
            [
                Text(""),
                Text("Authoritative drilldown payload", style="bold"),
                extra,
                Text(""),
                Text("Candidate readiness", style="bold"),
                render_readiness_items(drilldown.readiness_items),
                Text(""),
                Text("Candidate guidance", style="bold"),
                render_guidance(drilldown.guidance),
            ]
        )
    else:
        sections.extend(
            [
                Text(""),
                Text("Only row-level payload is available for this candidate.", style="dim"),
            ]
        )

    return Panel(Group(*sections), title="Drilldown", border_style="magenta")


__all__ = [
    "FilterSelection",
    "filter_rows",
    "render_candidate_panel",
    "render_operational_panel",
    "render_summary",
    "resolve_drilldown",
    "select_options",
]
