from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

from refactorq.adapters.typescript import TypeScriptAdapter
from refactorq.core.filesystem import walk_source_files
from refactorq.core.repo import detect_repo

from .models import VerificationCheckResult, VerificationKind, VerificationResult

_COMMAND_TIMEOUT_SECONDS = 120


def _npm_command() -> str:
    return "npm.cmd" if sys.platform == "win32" else "npm"


def _verify_python_parse(root: Path) -> VerificationCheckResult:
    errors: list[str] = []
    file_count = 0
    for path in walk_source_files(root, (".py",)):
        file_count += 1
        source = path.read_text(encoding="utf-8")
        try:
            ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            location = f"{path.relative_to(root).as_posix()}:{exc.lineno or 0}:{exc.offset or 0}"
            errors.append(f"{location} {exc.msg}")
    return VerificationCheckResult(
        name="python_parse",
        kind="parse",
        status="failed" if errors else "passed",
        evidence=errors[:20] if errors else [f"parsed {file_count} Python files"],
        details={"fileCount": file_count, "errorCount": len(errors)},
    )


def _verify_boundary_contracts(root: Path) -> VerificationCheckResult:
    repo = detect_repo(root)
    if not repo.mixed_language:
        return VerificationCheckResult(
            name="boundary_contracts",
            kind="build",
            status="skipped",
            evidence=["single-language repository; no cross-language boundary contract check required"],
            details={"mixedLanguage": False, "artifactCount": len(repo.boundary_artifacts)},
        )

    if not repo.boundary_artifacts:
        return VerificationCheckResult(
            name="boundary_contracts",
            kind="build",
            status="skipped",
            evidence=["mixed-language repository detected but no explicit boundary contract artifacts were found"],
            details={"mixedLanguage": True, "artifactCount": 0},
        )

    checked = 0
    failures: list[str] = []
    evidence: list[str] = []
    for artifact in repo.boundary_artifacts:
        checked += 1
        path = root / artifact
        suffix = path.suffix.lower()
        content = path.read_text(encoding="utf-8")
        if suffix == ".json":
            try:
                json.loads(content)
            except json.JSONDecodeError as exc:
                failures.append(f"{artifact}:{exc.lineno}:{exc.colno} invalid JSON boundary artifact")
                continue
            evidence.append(f"validated JSON boundary artifact: {artifact}")
            continue
        if path.name == ".env.example":
            invalid_lines = [
                f"line {index + 1}"
                for index, line in enumerate(content.splitlines())
                if line.strip() and not line.lstrip().startswith("#") and "=" not in line
            ]
            if invalid_lines:
                failures.append(f"{artifact} invalid env assignment format at {', '.join(invalid_lines[:5])}")
                continue
            evidence.append(f"validated env boundary artifact: {artifact}")
            continue
        if path.name in {"openapi.yaml", "openapi.yml"}:
            if "openapi:" not in content and "swagger:" not in content:
                failures.append(f"{artifact} does not look like an OpenAPI or Swagger document")
                continue
            evidence.append(f"validated OpenAPI boundary artifact marker: {artifact}")
            continue
        evidence.append(f"detected boundary artifact: {artifact}")

    return VerificationCheckResult(
        name="boundary_contracts",
        kind="build",
        status="failed" if failures else "passed",
        evidence=failures[:20] if failures else evidence,
        details={
            "mixedLanguage": True,
            "artifactCount": len(repo.boundary_artifacts),
            "checkedArtifactCount": checked,
            "failureCount": len(failures),
        },
    )


def _python_targets(root: Path) -> list[str]:
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


def _looks_like_missing_python_module(command: list[str], output: str) -> bool:
    if len(command) < 3 or command[0] != sys.executable or command[1] != "-m":
        return False
    module_name = command[2]
    missing_markers = (f"No module named {module_name}", f"No module named '{module_name}'")
    return any(marker in output for marker in missing_markers)


def _run_command_check(root: Path, *, name: str, kind: VerificationKind, command: list[str]) -> VerificationCheckResult:
    try:
        completed = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_COMMAND_TIMEOUT_SECONDS,
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
            evidence=[f"command timed out after {_COMMAND_TIMEOUT_SECONDS}s: {' '.join(command)}"],
            details={"command": command, "timeoutSeconds": _COMMAND_TIMEOUT_SECONDS},
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

    if _looks_like_missing_python_module(command, combined_output):
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


def _python_toolchain_checks(root: Path) -> list[VerificationCheckResult]:
    if not any(True for _ in walk_source_files(root, (".py",))):
        return []

    checks = [_run_command_check(root, name="python_lint", kind="lint", command=[sys.executable, "-m", "ruff", "check", "."])]
    targets = _python_targets(root)
    if targets:
        checks.append(
            _run_command_check(
                root,
                name="python_typecheck",
                kind="typecheck",
                command=[sys.executable, "-m", "mypy", *targets],
            )
        )
    test_dir = root / "tests"
    if test_dir.exists() and test_dir.is_dir():
        checks.append(
            _run_command_check(
                root,
                name="python_unit_tests",
                kind="unit_test",
                command=[sys.executable, "-m", "pytest", "-q"],
            )
        )
    return checks


def _package_script_checks(root: Path) -> list[VerificationCheckResult]:
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
    script_groups: list[tuple[str, VerificationKind, tuple[str, ...]]] = [
        ("typescript_lint", "lint", ("lint", "eslint")),
        ("typescript_typecheck", "typecheck", ("typecheck", "check", "ts:check")),
        ("typescript_build", "build", ("build", "ts:build")),
        ("typescript_unit_tests", "unit_test", ("test", "unit", "vitest", "jest")),
    ]
    for name, kind, choices in script_groups:
        script_name = next((choice for choice in choices if choice in scripts), None)
        if script_name is None:
            continue
        checks.append(
            _run_command_check(root, name=name, kind=kind, command=[_npm_command(), "run", script_name])
        )
    return checks


def verify_repo(root: Path) -> VerificationResult:
    checks: list[VerificationCheckResult] = []

    python_files = any(True for _ in walk_source_files(root, (".py",)))
    if python_files:
        checks.append(_verify_python_parse(root))
        checks.extend(_python_toolchain_checks(root))

    ts_adapter = TypeScriptAdapter()
    if ts_adapter.supports(root):
        checks.extend(ts_adapter.verify(root))
        checks.extend(_package_script_checks(root))

    checks.append(_verify_boundary_contracts(root))

    if len(checks) == 1 and checks[0].name == "boundary_contracts":
        checks.append(
            VerificationCheckResult(
                name="no_supported_checks",
                kind="parse",
                status="passed",
                evidence=["no supported Python or TypeScript sources detected"],
                details={"fileCount": 0},
            )
        )

    if any(check.status == "failed" for check in checks):
        return VerificationResult(status="failed", checks=checks)
    return VerificationResult(status="passed", checks=checks)