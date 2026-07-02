from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from refactorq.tui.widgets import render_guidance, render_readiness_items

if TYPE_CHECKING:
    from refactorq.core.tui.models import DoctorReport

_APP_EXPORTS = {"RefactorQTuiApp", "create_tui_app", "launch_tui"}
__all__ = [*sorted(_APP_EXPORTS), "render_doctor_report"]


def render_doctor_report(report: "DoctorReport", *, console: Console | None = None) -> None:
    target_console = console or Console()
    repo_root = report.source.repo_root or report.source.source
    snapshot = Table(show_header=True, expand=True)
    snapshot.add_column("Field", style="bold cyan")
    snapshot.add_column("Value", overflow="fold")
    snapshot.add_row("repo", repo_root)
    snapshot.add_row("source kind", report.source.source_kind)
    snapshot.add_row("working root", report.source.working_root or "-")
    snapshot.add_row("mutable", "yes" if report.source.mutable else "no")
    snapshot.add_row("preserved", "yes" if report.source.preserved else "no")
    if report.repo is not None:
        snapshot.add_row("languages", ", ".join(report.repo.languages) or "-")
        snapshot.add_row("toolchains", ", ".join(report.repo.toolchain) or "-")
        snapshot.add_row("python files", str(report.repo.python_files))
        snapshot.add_row("typescript files", str(report.repo.typescript_files))
        snapshot.add_row("javascript files", str(report.repo.javascript_files))
        snapshot.add_row("boundary artifacts", ", ".join(report.repo.boundary_artifacts) or "-")
    snapshot.add_row("selected candidates", str(report.facts.selected_count))
    snapshot.add_row("excluded candidates", str(report.facts.excluded_count))
    target_console.print(Panel.fit(f"RefactorQ Doctor: {repo_root}", style="bold blue"))
    target_console.print(Panel(snapshot, title="Repository", border_style="cyan"))
    target_console.print(
        Panel(
            Group(
                Text("Operational readiness and report-mode guidance.", style="dim"),
                render_guidance(report.guidance),
                Text(""),
                render_readiness_items(report.readiness_items),
            ),
            title="Doctor Summary",
            border_style="green",
        )
    )


def __getattr__(name: str) -> Any:
    if name in _APP_EXPORTS:
        module = import_module("refactorq.tui.app")
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
