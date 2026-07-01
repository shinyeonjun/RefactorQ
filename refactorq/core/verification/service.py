from __future__ import annotations

import ast
from pathlib import Path

from refactorq.core.repo import detect_repo
from refactorq.adapters.typescript import TypeScriptAdapter
from refactorq.core.filesystem import walk_source_files

from .models import VerificationCheckResult, VerificationResult


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
    if repo.boundary_artifacts:
        return VerificationCheckResult(
            name="boundary_contracts",
            kind="build",
            status="passed",
            evidence=[f"detected boundary artifacts: {', '.join(repo.boundary_artifacts)}"],
            details={"mixedLanguage": True, "artifactCount": len(repo.boundary_artifacts)},
        )
    return VerificationCheckResult(
        name="boundary_contracts",
        kind="build",
        status="skipped",
        evidence=["mixed-language repository detected but no explicit boundary contract artifacts were found"],
        details={"mixedLanguage": True, "artifactCount": 0},
    )

def verify_repo(root: Path) -> VerificationResult:
    checks: list[VerificationCheckResult] = []

    python_files = any(True for _ in walk_source_files(root, (".py",)))
    if python_files:
        checks.append(_verify_python_parse(root))

    ts_adapter = TypeScriptAdapter()
    if ts_adapter.supports(root):
        checks.extend(ts_adapter.verify(root))

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