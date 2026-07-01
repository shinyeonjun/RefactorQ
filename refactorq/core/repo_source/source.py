from __future__ import annotations

import atexit
import json
import shutil
import stat
import tempfile
import zipfile
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse
from urllib.request import Request, urlopen


_GITHUB_API_ACCEPT = "application/vnd.github+json"
_USER_AGENT = "refactorq"
_DEFERRED_CLEANUP: set[Path] = set()
_DEFERRED_CLEANUP_RECORD = Path(tempfile.gettempdir()) / "refactorq-repo-source-cleanup.json"


@dataclass(frozen=True)
class NormalizedRepoSource:
    original: str
    analysis_root: Path
    kind: str


@contextmanager
def normalize_repo_source(source: str | Path) -> Iterator[NormalizedRepoSource]:
    _drain_deferred_cleanup()
    if isinstance(source, Path):
        yield NormalizedRepoSource(original=str(source), analysis_root=source.resolve(), kind="local")
        return

    if _is_github_repo_url(source):
        with _normalize_github_repo_source(source) as repo_source:
            yield repo_source
        return

    local_path = Path(source).expanduser()
    if not local_path.exists():
        raise FileNotFoundError(source)
    if not local_path.is_dir():
        raise NotADirectoryError(source)
    yield NormalizedRepoSource(original=source, analysis_root=local_path.resolve(), kind="local")


@contextmanager
def _normalize_github_repo_source(source: str) -> Iterator[NormalizedRepoSource]:
    temp_root = Path(tempfile.mkdtemp(prefix="refactorq-repo-source-"))
    archive_path = temp_root / "source.zip"
    extract_path = temp_root / "extract"
    try:
        archive_url = resolve_github_archive_url(source)
        _download_file(archive_url, archive_path)
        extract_path.mkdir()
        analysis_root = _extract_archive(archive_path, extract_path)
        _mark_read_only(analysis_root)
        yield NormalizedRepoSource(original=source, analysis_root=analysis_root, kind="github")
    finally:
        _cleanup_now_or_defer(temp_root)


def _is_github_repo_url(source: str) -> bool:
    parsed = urlparse(source)
    return parsed.scheme in {"http", "https"} and parsed.netloc.lower() == "github.com"


def resolve_github_archive_url(source: str) -> str:
    owner, repo = _parse_github_repo(source)
    default_branch = _fetch_default_branch(owner, repo)
    return f"https://github.com/{owner}/{repo}/archive/refs/heads/{default_branch}.zip"


def _parse_github_repo(source: str) -> tuple[str, str]:
    parsed = urlparse(source)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        raise ValueError(f"Unsupported GitHub repository URL: {source}")
    owner = parts[0]
    repo = parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not owner or not repo:
        raise ValueError(f"Unsupported GitHub repository URL: {source}")
    return owner, repo


def _fetch_default_branch(owner: str, repo: str) -> str:
    metadata = _fetch_json(f"https://api.github.com/repos/{owner}/{repo}")
    default_branch = metadata.get("default_branch")
    if not isinstance(default_branch, str) or not default_branch:
        raise ValueError(f"GitHub repository metadata missing default_branch for {owner}/{repo}")
    return default_branch


def _fetch_json(url: str) -> dict[str, object]:
    request = Request(
        url,
        headers={
            "Accept": _GITHUB_API_ACCEPT,
            "User-Agent": _USER_AGENT,
        },
    )
    with urlopen(request) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError(f"Unexpected JSON payload from {url}")
    return payload


def _download_file(url: str, destination: Path) -> Path:
    request = Request(url, headers={"User-Agent": _USER_AGENT})
    with urlopen(request) as response, destination.open("wb") as handle:
        shutil.copyfileobj(response, handle)
    return destination


def _extract_archive(archive_path: Path, destination: Path) -> Path:
    with zipfile.ZipFile(archive_path) as archive:
        archive.extractall(destination)

    extracted_roots = [path for path in destination.iterdir() if path.is_dir()]
    if len(extracted_roots) != 1:
        raise ValueError(f"Expected a single extracted repository root in {archive_path}")
    return extracted_roots[0]


def _mark_read_only(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_dir():
            path.chmod(stat.S_IREAD | stat.S_IEXEC)
            continue
        path.chmod(stat.S_IREAD)
    root.chmod(stat.S_IREAD | stat.S_IEXEC)


def _cleanup_now_or_defer(path: Path) -> None:
    if not path.exists():
        return
    try:
        _remove_tree(path)
    except OSError:
        _defer_cleanup(path)


def _defer_cleanup(path: Path) -> None:
    if path in _DEFERRED_CLEANUP:
        return
    _DEFERRED_CLEANUP.add(path)
    _persist_deferred_cleanup()
    atexit.register(_cleanup_deferred_path, path)


def _cleanup_deferred_path(path: Path) -> None:
    try:
        if path.exists():
            _remove_tree(path)
    except OSError:
        pass
    finally:
        _DEFERRED_CLEANUP.discard(path)
        _persist_deferred_cleanup()


def _persist_deferred_cleanup() -> None:
    if not _DEFERRED_CLEANUP:
        if _DEFERRED_CLEANUP_RECORD.exists():
            _DEFERRED_CLEANUP_RECORD.unlink()
        return
    payload = sorted(str(path) for path in _DEFERRED_CLEANUP)
    _DEFERRED_CLEANUP_RECORD.write_text(json.dumps(payload), encoding="utf-8")


def _drain_deferred_cleanup() -> None:
    if _DEFERRED_CLEANUP_RECORD.exists():
        try:
            payload = json.loads(_DEFERRED_CLEANUP_RECORD.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = []
        if isinstance(payload, list):
            for value in payload:
                if isinstance(value, str):
                    _DEFERRED_CLEANUP.add(Path(value))
    pending = list(_DEFERRED_CLEANUP)
    for path in pending:
        try:
            if path.exists():
                _remove_tree(path)
        except OSError:
            continue
        _DEFERRED_CLEANUP.discard(path)
    _persist_deferred_cleanup()


def _remove_tree(path: Path) -> None:
    shutil.rmtree(path, onerror=_handle_remove_error)


def _handle_remove_error(func: Callable[[str], object], path: str, _: tuple[object, object, object]) -> None:
    Path(path).chmod(stat.S_IWRITE)
    func(path)
