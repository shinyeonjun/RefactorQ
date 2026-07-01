from __future__ import annotations

from pathlib import Path
from typing import Iterable

IGNORED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "coverage",
}


def walk_repo_files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in IGNORED_DIRS for part in path.parts):
            continue
        yield path


def walk_source_files(root: Path, extensions: tuple[str, ...]) -> Iterable[Path]:
    for path in walk_repo_files(root):
        if path.suffix in extensions:
            yield path
