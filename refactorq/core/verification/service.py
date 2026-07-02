from __future__ import annotations

import ast
from pathlib import Path
from typing import Literal, Sequence, cast


from refactorq.adapters.registry import detect_adapters
from refactorq.adapters.typescript import TypeScriptAdapter as _TypeScriptAdapter
from refactorq.core.candidate.models import Candidate, VerificationCheck
from refactorq.core.filesystem import walk_source_files

from .boundary_checks import contract_markers, first_contract_marker_match, verify_boundary_contracts
from .command_checks import package_script_checks, python_toolchain_checks
from .models import VerificationCheckResult, VerificationKind, VerificationResult
from .readiness import (
    build_verification_plan as build_verification_plan,
    build_verification_readiness as build_verification_readiness,
    build_verification_report as build_verification_report,
    candidate_verification_state as candidate_verification_state,
    ordered_unique,
)
from .proofs import verification_result_from_report


TypeScriptAdapter = _TypeScriptAdapter


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


def _boundary_integration_check(root: Path, candidates: Sequence[Candidate]) -> VerificationCheckResult | None:
    relevant = [candidate for candidate in candidates if "integration_test" in candidate.required_checks]
    if not relevant:
        return None

    failures: list[str] = []
    evidence: list[str] = []
    for candidate in relevant:
        state = candidate_verification_state(root, candidate)
        missing_artifacts = cast(list[str], state["missingArtifacts"])
        producer_side = cast(list[str], state["producerSide"])
        consumer_side = cast(list[str], state["consumerSide"])
        contract_artifacts = cast(list[str], state["contractArtifacts"])
        if not contract_artifacts:
            failures.append(f"{candidate.id} missing boundary contract artifacts for integration coverage")
            continue
        if missing_artifacts:
            failures.append(f"{candidate.id} missing boundary contract artifacts: {', '.join(missing_artifacts)}")
            continue
        if not producer_side:
            failures.append(f"{candidate.id} missing explicit producer-side files for integration coverage")
            continue
        if not consumer_side:
            failures.append(f"{candidate.id} missing explicit consumer-side files for integration coverage")
            continue

        markers = ordered_unique(marker for artifact in contract_artifacts for marker in contract_markers(root, artifact))
        producer_match = first_contract_marker_match(root, producer_side, markers) if markers else None
        consumer_match = first_contract_marker_match(root, consumer_side, markers) if markers else None
        if markers and producer_match is None:
            failures.append(f"{candidate.id} producer-side files do not reference any contract markers from {', '.join(contract_artifacts)}")
            continue
        if markers and consumer_match is None:
            failures.append(f"{candidate.id} consumer-side files do not reference any contract markers from {', '.join(contract_artifacts)}")
            continue

        marker_summary = (
            f" producerMatch={producer_match[0]}:{producer_match[1]} consumerMatch={consumer_match[0]}:{consumer_match[1]}"
            if producer_match and consumer_match
            else ""
        )
        evidence.append(
            f"{candidate.id} contract execution surfaces producer={','.join(producer_side)} consumer={','.join(consumer_side)} artifacts={','.join(contract_artifacts)}{marker_summary}"
        )

    return VerificationCheckResult(
        name="boundary_integration",
        kind="integration_test",
        status="failed" if failures else "passed",
        evidence=failures[:20] if failures else evidence,
        details={"candidateCount": len(relevant), "candidateIds": [candidate.id for candidate in relevant]},
    )


def _required_check_coverage(
    root: Path,
    *,
    required_checks: Sequence[str],
    candidates: Sequence[Candidate],
    checks: list[VerificationCheckResult],
) -> list[VerificationCheckResult]:
    extra_checks: list[VerificationCheckResult] = []
    boundary_integration = _boundary_integration_check(root, candidates)
    if boundary_integration is not None:
        extra_checks.append(boundary_integration)

    required: list[VerificationKind] = [cast(VerificationKind, check) for check in ordered_unique(required_checks)]
    all_checks = [*checks, *extra_checks]
    for kind in required:
        relevant = [check for check in all_checks if check.kind == kind]
        if not relevant:
            required_by = [candidate.id for candidate in candidates if kind in candidate.required_checks]
            extra_checks.append(
                VerificationCheckResult(
                    name=f"required_{kind}_coverage",
                    kind=kind,
                    status="failed",
                    evidence=[f"required verification check was not executed: {kind}"],
                    details={"requiredBy": required_by},
                )
            )
            continue
        if all(check.status == "skipped" for check in relevant):
            required_by = [candidate.id for candidate in candidates if kind in candidate.required_checks]
            extra_checks.append(
                VerificationCheckResult(
                    name=f"required_{kind}_coverage",
                    kind=kind,
                    status="failed",
                    evidence=[f"required verification check was only skipped: {kind}"],
                    details={"requiredBy": required_by},
                )
            )
    return extra_checks


def verify_repo(
    root: Path,
    *,
    required_checks: Sequence[VerificationCheck] | None = None,
    candidates: Sequence[Candidate] | None = None,
) -> VerificationResult:
    checks: list[VerificationCheckResult] = []
    scoped_candidates = [] if candidates is None else list(candidates)
    verification_report = build_verification_report(
        root,
        required_checks=[] if required_checks is None else list(required_checks),
        candidates=scoped_candidates,
    )
    python_files = any(True for _ in walk_source_files(root, (".py",)))
    if python_files:
        checks.append(_verify_python_parse(root))
        checks.extend(python_toolchain_checks(root))

    adapters = detect_adapters(root)
    for adapter in adapters:
        checks.extend(adapter.verify(root))
    if any(adapter.metadata.language == "typescript" for adapter in adapters):
        checks.extend(package_script_checks(root))

    checks.append(verify_boundary_contracts(root))

    if required_checks:
        checks.extend(
            _required_check_coverage(
                root,
                required_checks=cast(list[str], verification_report["requiredChecks"]),
                candidates=scoped_candidates,
                checks=checks,
            )
        )

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

    status: Literal["passed", "failed"] = "failed" if any(check.status == "failed" for check in checks) else "passed"
    return verification_result_from_report(status=status, checks=checks, report=verification_report)
