from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from refactorq.core.filesystem import walk_source_files

from .models import VerificationCheckResult, VerificationKind


COMMAND_TIMEOUT_SECONDS = 120
SCRIPT_GROUPS: list[tuple[str, VerificationKind, tuple[str, ...]]] = [
    ("typescript_lint", "lint", ("lint", "eslint")),
    ("typescript_typecheck", "typecheck", ("typecheck", "check", "ts:check")),
    ("typescript_build", "build", ("build", "ts:build")),
    ("typescript_unit_tests", "unit_test", ("test", "unit", "vitest", "jest")),
]


def package_scripts(root: Path) -> dict[str, str]:
    package_json = root / "package.json"
    if not package_json.exists():
        return {}
    try:
        payload = json.loads(package_json.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    scripts = payload.get("scripts")
    return scripts if isinstance(scripts, dict) else {}


def python_toolchain_checks(root: Path) -> list[VerificationCheckResult]:
    if not any(True for _ in walk_source_files(root, (".py",))):
        return []

    checks = [run_command_check(root, name="python_lint", kind="lint", command=[sys.executable, "-m", "ruff", "check", "."])]
    targets = python_targets(root)
    if targets:
        checks.append(
            run_command_check(
                root,
                name="python_typecheck",
                kind="typecheck",
                command=[sys.executable, "-m", "mypy", *targets],
            )
        )
    test_dir = root / "tests"
    if test_dir.exists() and test_dir.is_dir():
        checks.append(
            run_command_check(
                root,
                name="python_unit_tests",
                kind="unit_test",
                command=[sys.executable, "-m", "pytest", "-q"],
            )
        )
    return checks


def package_script_checks(root: Path) -> list[VerificationCheckResult]:
    package_json = root / "package.json"
    if not package_json.exists():
        return []
    try:
        payload = json.loads(package_json.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return [
            VerificationCheckResult(
                name="package_json_parse",
                kind="build",
                status="failed",
                evidence=["package.json is not valid JSON"],
                details={"path": str(package_json)},
            )
        ]

    scripts = payload.get("scripts")
    if not isinstance(scripts, dict):
        return []

    checks: list[VerificationCheckResult] = []
    for name, kind, choices in SCRIPT_GROUPS:
        script_name = next((choice for choice in choices if choice in scripts), None)
        if script_name is None:
            continue
        checks.append(run_command_check(root, name=name, kind=kind, command=[npm_command(), "run", script_name]))
    return checks


def npm_command() -> str:
    return "npm.cmd" if sys.platform == "win32" else "npm"


def python_targets(root: Path) -> list[str]:
    targets: list[str] = []
    for path in sorted(root.iterdir()):
        if path.name.startswith("."):
            continue
        if path.is_file() and path.suffix == ".py":
            targets.append(path.name)
            continue
        if not path.is_dir():
            continue
        if any(True for _ in walk_source_files(path, (".py",))):
            targets.append(path.name)
    return targets


def looks_like_missing_python_module(command: list[str], output: str) -> bool:
    if len(command) < 3 or command[0] != sys.executable or command[1] != "-m":
        return False
    module_name = command[2]
    missing_markers = (f"No module named {module_name}", f"No module named '{module_name}'")
    return any(marker in output for marker in missing_markers)


def run_command_check(root: Path, *, name: str, kind: VerificationKind, command: list[str]) -> VerificationCheckResult:
    try:
        completed = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError:
        return VerificationCheckResult(
            name=name,
            kind=kind,
            status="skipped",
            evidence=[f"command not available: {' '.join(command)}"],
            details={"command": command},
        )
    except subprocess.TimeoutExpired:
        return VerificationCheckResult(
            name=name,
            kind=kind,
            status="failed",
            evidence=[f"command timed out after {COMMAND_TIMEOUT_SECONDS}s: {' '.join(command)}"],
            details={"command": command, "timeoutSeconds": COMMAND_TIMEOUT_SECONDS},
        )

    stdout = completed.stdout.strip()
    stderr = completed.stderr.strip()
    combined_output = "\n".join(part for part in (stdout, stderr) if part)
    if completed.returncode == 0:
        evidence = [f"command passed: {' '.join(command)}"]
        if stdout:
            evidence.extend(stdout.splitlines()[:10])
        return VerificationCheckResult(
            name=name,
            kind=kind,
            status="passed",
            evidence=evidence,
            details={"command": command, "returnCode": completed.returncode},
        )

    if looks_like_missing_python_module(command, combined_output):
        return VerificationCheckResult(
            name=name,
            kind=kind,
            status="skipped",
            evidence=[f"python module for verification is not installed: {' '.join(command[:3])}"],
            details={"command": command, "returnCode": completed.returncode},
        )

    evidence = [f"command failed ({completed.returncode}): {' '.join(command)}"]
    if combined_output:
        evidence.extend(combined_output.splitlines()[:20])
    return VerificationCheckResult(
        name=name,
        kind=kind,
        status="failed",
        evidence=evidence,
        details={"command": command, "returnCode": completed.returncode},
    )
