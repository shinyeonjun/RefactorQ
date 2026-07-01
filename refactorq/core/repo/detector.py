from __future__ import annotations

from pathlib import Path
from typing import Iterable

from .models import RepoManifestMap, RepoSnapshot

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
BOUNDARY_FILENAMES = {
    "openapi.yaml",
    "openapi.yml",
    "swagger.json",
    "schema.json",
    ".env.example",
}


def _iter_files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in IGNORED_DIRS for part in path.parts):
            continue
        yield path


def detect_repo(root: Path) -> RepoSnapshot:
    files = list(_iter_files(root))
    python_files = sum(1 for path in files if path.suffix == ".py")
    typescript_files = sum(1 for path in files if path.suffix == ".ts")
    javascript_files = sum(1 for path in files if path.suffix == ".js")

    manifests = RepoManifestMap(
        pyproject=(root / "pyproject.toml").exists(),
        packageJson=(root / "package.json").exists(),
        tsconfig=(root / "tsconfig.json").exists() or any(path.name == "tsconfig.json" for path in files),
        requirementsTxt=(root / "requirements.txt").exists(),
        poetryLock=(root / "poetry.lock").exists(),
        uvLock=(root / "uv.lock").exists(),
    )

    languages: list[str] = []
    if python_files:
        languages.append("python")
    if typescript_files:
        languages.append("typescript")
    if javascript_files:
        languages.append("javascript")
    if not languages:
        languages.append("unknown")

    toolchain: list[str] = []
    if manifests.pyproject or manifests.requirements_txt or manifests.poetry_lock or manifests.uv_lock:
        toolchain.extend(["python", "ruff", "mypy", "pytest"])
    if manifests.package_json:
        toolchain.extend(["node", "typescript", "eslint"])
    toolchain = sorted(set(toolchain))

    boundary_artifacts = sorted(
        str(path.relative_to(root)).replace("\\", "/")
        for path in files
        if path.name in BOUNDARY_FILENAMES
    )

    return RepoSnapshot(
        root=str(root.resolve()),
        pythonFiles=python_files,
        typescriptFiles=typescript_files,
        javascriptFiles=javascript_files,
        manifests=manifests,
        toolchain=toolchain,
        languages=languages,
        mixedLanguage=len([lang for lang in languages if lang != "unknown"]) > 1,
        boundaryArtifacts=boundary_artifacts,
    )
