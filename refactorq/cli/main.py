from __future__ import annotations


import orjson

import typer
from rich.console import Console

from refactorq.core.planning import PlanMode
from refactorq.core.service import RefactorQService

app = typer.Typer(help="RefactorQ repository refactoring orchestrator.")
console = Console()
service = RefactorQService()


def _emit_json(payload: object) -> None:
    console.file.write(orjson.dumps(payload).decode("utf-8"))
    console.file.write("\n")
    console.file.flush()

@app.command()
def scan(repo: str = typer.Argument(...)) -> None:
    """Inspect a repository and report detected languages, adapters, and candidates."""
    result = service.scan_source(repo)
    _emit_json(result.model_dump(by_alias=True))


@app.command()
def plan(
    repo: str = typer.Argument(...),
    mode: PlanMode = typer.Option("safe", "--mode"),
) -> None:
    """Create a ranked planning payload from the current repository scan."""
    result = service.plan_source(repo, mode)
    _emit_json(result.model_dump(by_alias=True))


@app.command()
def apply(
    repo: str = typer.Argument(...),
    mode: PlanMode = typer.Option("safe", "--mode"),
) -> None:
    """Apply deterministic low-risk refactors for the selected plan."""
    result = service.apply_source(repo, mode)
    _emit_json(result.model_dump(by_alias=True))


@app.command()
def verify(repo: str = typer.Argument(...)) -> None:
    """Run structural verification for supported repository languages."""
    result = service.verify_source(repo)
    _emit_json(result.model_dump(by_alias=True))


@app.command()
def report(
    repo: str = typer.Argument(...),
    mode: PlanMode = typer.Option("report", "--mode", case_sensitive=False),
) -> None:
    """Summarize plan output and current deterministic execution support."""
    result = service.report_source(repo, mode)
    _emit_json(result.model_dump(by_alias=True))

@app.command()
def doctor(repo: str = typer.Argument(...)) -> None:
    """Inspect repository readiness for report-only terminal review."""
    from refactorq.tui import render_doctor_report

    report = service.doctor_source(repo)
    render_doctor_report(report, console=console)


@app.command()
def tui(repo: str = typer.Argument(...)) -> None:
    """Open the report-only terminal candidate browser."""
    from refactorq.tui import launch_tui

    payload = service.tui_source(repo)
    try:
        launch_tui(payload)
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc


@app.command(name="run")
def run_pipeline(
    repo: str = typer.Argument(...),
    mode: PlanMode = typer.Option("safe", "--mode"),
) -> None:
    """Plan, apply deterministic refactors, verify, and roll back on failure."""
    result = service.run_source(repo, mode)
    _emit_json(result.model_dump(by_alias=True))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
