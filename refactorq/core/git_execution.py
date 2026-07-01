from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class GitWorkspaceState:
    available: bool
    repo_root: Path | None
    base_branch: str | None
    clean: bool
    reason: str | None = None


@dataclass(slots=True)
class GitExecutionContext:
    repo_root: Path
    base_branch: str
    execution_branch: str
    created_branch: bool


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )


def inspect_git_workspace(root: Path) -> GitWorkspaceState:
    try:
        repo_root_output = _git(root, "rev-parse", "--show-toplevel").stdout.strip()
        repo_root = Path(repo_root_output)
        base_branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "HEAD"
        clean = _git(root, "status", "--short").stdout.strip() == ""
        reason = None if clean else "git worktree is not clean"
        return GitWorkspaceState(
            available=True,
            repo_root=repo_root,
            base_branch=base_branch,
            clean=clean,
            reason=reason,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return GitWorkspaceState(
            available=False,
            repo_root=None,
            base_branch=None,
            clean=False,
            reason="git workspace is unavailable",
        )


def _branch_exists(root: Path, branch_name: str) -> bool:
    try:
        _git(root, "rev-parse", "--verify", branch_name)
        return True
    except subprocess.CalledProcessError:
        return False


def _unique_execution_branch(root: Path, mode: str) -> str:
    head = _git(root, "rev-parse", "--short", "HEAD").stdout.strip() or "head"
    base = f"refactorq/{mode}-{head}"
    if not _branch_exists(root, base):
        return base
    suffix = 2
    while _branch_exists(root, f"{base}-{suffix}"):
        suffix += 1
    return f"{base}-{suffix}"


def begin_git_execution(root: Path, mode: str) -> GitExecutionContext | None:
    state = inspect_git_workspace(root)
    if not state.available or not state.clean or state.repo_root is None or state.base_branch is None:
        return None
    execution_branch = _unique_execution_branch(root, mode)
    _git(root, "checkout", "-b", execution_branch)
    return GitExecutionContext(
        repo_root=state.repo_root,
        base_branch=state.base_branch,
        execution_branch=execution_branch,
        created_branch=True,
    )


def finalize_git_execution(root: Path, context: GitExecutionContext, changed_files: list[str], mode: str) -> str:
    if not changed_files:
        raise ValueError("cannot finalize git execution without changed files")
    _git(root, "add", "--", *changed_files)
    _git(root, "commit", "-m", f"refactorq: apply {mode} execution")
    return _git(root, "rev-parse", "HEAD").stdout.strip()


def abort_git_execution(root: Path, context: GitExecutionContext) -> None:
    _git(root, "checkout", context.base_branch)
    if context.created_branch:
        _git(root, "branch", "-D", context.execution_branch)
