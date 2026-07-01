from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.json import JSON

from refactorq.core.planning import PlanMode
from refactorq.core.service import RefactorQService

app = typer.Typer(help="RefactorQ repository refactoring orchestrator.")
console = Console()
service = RefactorQService()


def _emit_json(payload: object) -> None:
    console.print(JSON.from_data(payload))


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
    repo: Path = typer.Argument(..., exists=True, file_okay=False, dir_okay=True),
    mode: str = typer.Option("safe", "--mode"),
) -> None:
    """Emit the current apply scaffold status for a repository."""
    result = service.scan(repo)
    _emit_json(
        {
            "mode": mode,
            "repo": result.repo.model_dump(by_alias=True),
            "status": "apply pipeline scaffolded",
            "candidateCount": len(result.candidates),
        }
    )


@app.command()
def verify(
    repo: Path = typer.Argument(..., exists=True, file_okay=False, dir_okay=True),
) -> None:
    """Emit the current verification scaffold status for a repository."""
    result = service.scan(repo)
    _emit_json(
        {
            "repo": result.repo.model_dump(by_alias=True),
            "status": "verification scaffolded",
            "checks": ["parse", "lint", "typecheck", "build", "unit_test"],
        }
    )


@app.command()
def report(
    repo: Path = typer.Argument(..., exists=True, file_okay=False, dir_okay=True),
    mode: str = typer.Option("report", "--mode"),
) -> None:
    """Emit the current report scaffold status for a repository."""
    result = service.scan(repo)
    _emit_json(
        {
            "mode": mode,
            "repo": result.repo.model_dump(by_alias=True),
            "status": "report scaffold ready",
            "candidateCount": len(result.candidates),
        }
    )


@app.command(name="run")
def run_pipeline(
    repo: Path = typer.Argument(..., exists=True, file_okay=False, dir_okay=True),
    mode: str = typer.Option("safe", "--mode"),
) -> None:
    """Run the current scan-first pipeline scaffold."""
    result = service.scan(repo)
    _emit_json(
        {
            "mode": mode,
            "repo": result.repo.model_dump(by_alias=True),
            "adapters": result.adapter_names,
            "candidateCount": len(result.candidates),
            "status": "run scaffold ready",
        }
    )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
