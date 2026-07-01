from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Literal, Sequence, cast


from refactorq.adapters.registry import detect_adapters
from refactorq.adapters.typescript import TypeScriptAdapter as _TypeScriptAdapter
from refactorq.core.candidate.models import Candidate, VerificationCheck
from refactorq.core.filesystem import walk_source_files
from refactorq.core.repo import detect_repo

from .models import ProofRecord, VerificationCheckResult, VerificationKind, VerificationReadiness, VerificationResult


TypeScriptAdapter = _TypeScriptAdapter


_COMMAND_TIMEOUT_SECONDS = 120
_SOURCE_EXTENSIONS = (".py", ".ts", ".tsx", ".js", ".jsx")
_PRODUCER_TOKENS = {"api", "apis", "route", "routes", "controller", "controllers", "backend", "server"}
_CONSUMER_TOKENS = {"client", "clients", "frontend", "web", "sdk", "ui"}
_SCRIPT_GROUPS: list[tuple[str, VerificationKind, tuple[str, ...]]] = [
    ("typescript_lint", "lint", ("lint", "eslint")),
    ("typescript_typecheck", "typecheck", ("typecheck", "check", "ts:check")),
    ("typescript_build", "build", ("build", "ts:build")),
    ("typescript_unit_tests", "unit_test", ("test", "unit", "vitest", "jest")),
]
_BOUNDARY_ENFORCED_CHECKS = {"build", "integration_test"}



def _npm_command() -> str:
    return "npm.cmd" if sys.platform == "win32" else "npm"


def _ordered_unique(items: Iterable[str]) -> list[str]:
    ordered: dict[str, None] = {}
    for item in items:
        ordered.setdefault(item, None)
    return list(ordered)


def _path_tokens(rel_path: str) -> set[str]:
    path = PurePosixPath(rel_path)
    tokens = {part.lower() for part in path.parts}
    stem = path.stem.lower()
    if stem:
        tokens.add(stem)
    return tokens


def _package_scripts(root: Path) -> dict[str, str]:
    package_json = root / "package.json"
    if not package_json.exists():
        return {}
    try:
        payload = json.loads(package_json.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    scripts = payload.get("scripts")
    return scripts if isinstance(scripts, dict) else {}


def _openapi_contract_markers(content: str) -> list[str]:
    markers: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("/") and stripped.endswith(":"):
            markers.append(stripped[:-1])
            continue
        if stripped.startswith("operationId:"):
            marker = stripped.split(":", 1)[1].strip()
            if marker:
                markers.append(marker)
    return _ordered_unique(markers)


def _contract_markers(root: Path, artifact: str) -> list[str]:
    path = root / artifact
    if not path.exists():
        return []
    content = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if path.name in {"openapi.yaml", "openapi.yml"}:
        return _openapi_contract_markers(content)
    if suffix == ".json":
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            return []
        markers: list[str] = []
        if isinstance(payload, dict):
            if isinstance(payload.get("title"), str):
                markers.append(payload["title"])
            properties = payload.get("properties")
            if isinstance(properties, dict):
                markers.extend(str(key) for key in properties)
        return _ordered_unique(markers)
    if path.name == ".env.example":
        return _ordered_unique(
            line.split("=", 1)[0].strip()
            for line in content.splitlines()
            if line.strip() and not line.lstrip().startswith("#") and "=" in line
        )
    return []


def _first_contract_marker_match(root: Path, rel_paths: Sequence[str], markers: Sequence[str]) -> tuple[str, str] | None:
    lowered_markers = [(marker, marker.lower()) for marker in markers if marker]
    for rel_path in rel_paths:
        path = root / rel_path
        if not path.exists():
            continue
        content = path.read_text(encoding="utf-8").lower()
        for marker, lowered in lowered_markers:
            if lowered in content:
                return rel_path, marker
    return None


def _verification_capabilities(root: Path) -> set[VerificationCheck]:
    capabilities: set[VerificationCheck] = set()
    python_files = any(True for _ in walk_source_files(root, (".py",)))
    if python_files:
        capabilities.update({"parse", "lint", "typecheck"})
        test_dir = root / "tests"
        if test_dir.exists() and test_dir.is_dir():
            capabilities.add("unit_test")

    for adapter in detect_adapters(root):
        capabilities.update(cast(set[VerificationCheck], set(adapter.metadata.verification_checks)))

    scripts = _package_scripts(root)
    for _, kind, choices in _SCRIPT_GROUPS:
        if any(choice in scripts for choice in choices):
            capabilities.add(kind)

    repo = detect_repo(root)
    if repo.mixed_language and repo.boundary_artifacts:
        capabilities.add("build")
    return capabilities



def _repo_boundary_sides(root: Path) -> tuple[list[str], list[str]]:
    producers: list[str] = []
    consumers: list[str] = []
    for path in walk_source_files(root, _SOURCE_EXTENSIONS):
        rel_path = path.relative_to(root).as_posix()
        tokens = _path_tokens(rel_path)
        if tokens & _PRODUCER_TOKENS:
            producers.append(rel_path)
        if tokens & _CONSUMER_TOKENS:
            consumers.append(rel_path)
    return _ordered_unique(producers), _ordered_unique(consumers)


def _existing_relative_paths(root: Path, rel_paths: Iterable[str]) -> list[str]:
    existing: list[str] = []
    for rel_path in _ordered_unique(rel_paths):
        if (root / rel_path).exists():
            existing.append(rel_path)
    return existing


def _resolved_boundary_sides(root: Path, candidate: Candidate) -> tuple[list[str], list[str]]:
    producer_side = _existing_relative_paths(root, candidate.boundary_impact.producer_side)
    consumer_side = _existing_relative_paths(root, candidate.boundary_impact.consumer_side)
    repo_producers, repo_consumers = _repo_boundary_sides(root)

    if not producer_side:
        if candidate.language == "python":
            producer_side = _existing_relative_paths(root, candidate.files)
        elif candidate.boundary_impact.cross_language:
            producer_side = [path for path in repo_producers if path not in consumer_side]

    if not consumer_side:
        if candidate.language in {"typescript", "javascript"}:
            consumer_side = _existing_relative_paths(root, candidate.files)
        elif candidate.boundary_impact.cross_language:
            consumer_side = [path for path in repo_consumers if path not in producer_side]

    if candidate.boundary_impact.cross_language and not producer_side:
        producer_side = [path for path in repo_producers if path not in consumer_side]
    if candidate.boundary_impact.cross_language and not consumer_side:
        consumer_side = [path for path in repo_consumers if path not in producer_side]

    return producer_side, consumer_side



def _proof_status(*, boundary_sensitive: bool, missing_predicates: Sequence[str], proof_refs: Sequence[str]) -> str:
    if missing_predicates:
        return "missing"
    if boundary_sensitive and proof_refs:
        return "proven"
    return "not_applicable"


def build_verification_readiness(root: Path, candidate: Candidate) -> dict[str, Any]:
    available_checks = set(_verification_capabilities(root))
    producer_side, consumer_side = _resolved_boundary_sides(root, candidate)
    contract_artifacts = _ordered_unique(candidate.boundary_impact.contract_artifacts)
    missing_artifacts = [artifact for artifact in contract_artifacts if not (root / artifact).exists()]
    blocked_reasons: list[str] = []

    if candidate.boundary_impact.cross_language:
        if not contract_artifacts:
            blocked_reasons.append("cross-language candidate requires explicit boundary contract artifacts")
        if missing_artifacts:
            blocked_reasons.append(
                "boundary contract artifacts are missing from the working tree: " + ", ".join(missing_artifacts)
            )
        if contract_artifacts:
            available_checks.add("build")
        if producer_side and consumer_side and contract_artifacts and not missing_artifacts:
            available_checks.add("integration_test")
        elif "integration_test" in candidate.required_checks:
            if not producer_side:
                blocked_reasons.append("integration_test requires explicit producer-side files")
            if not consumer_side:
                blocked_reasons.append("integration_test requires explicit consumer-side files")

    available_required_checks = sorted(check for check in candidate.required_checks if check in available_checks)
    boundary_sensitive = candidate.boundary_impact.cross_language or candidate.boundary_impact.impact_level != "none"
    if candidate.apply_mode_hint == "report_only" and boundary_sensitive:
        enforced_required_checks = list(candidate.required_checks)
    else:
        enforced_required_checks = (
            [check for check in candidate.required_checks if check in _BOUNDARY_ENFORCED_CHECKS]
            if boundary_sensitive
            else []
        )
    missing_required_checks = [check for check in enforced_required_checks if check not in available_checks]
    if missing_required_checks:
        blocked_reasons.append(
            "required verification checks are not available: " + ", ".join(missing_required_checks)
        )

    missing_predicates = _ordered_unique(
        [
            *missing_required_checks,
            *[f"artifact:{artifact}" for artifact in missing_artifacts],
            *( ["contract_artifacts"] if candidate.boundary_impact.cross_language and not contract_artifacts else [] ),
            *( ["producer_side"] if "integration_test" in candidate.required_checks and not producer_side else [] ),
            *( ["consumer_side"] if "integration_test" in candidate.required_checks and not consumer_side else [] ),
        ]
    )
    proof_refs = _ordered_unique(
        [
            *[f"check:{check}" for check in available_required_checks],
            *[f"artifact:{artifact}" for artifact in contract_artifacts if artifact not in missing_artifacts],
        ]
    )

    return {
        "candidateId": candidate.id,
        "kind": candidate.kind,
        "requiredChecks": list(candidate.required_checks),
        "availableChecks": available_required_checks,
        "missingRequiredChecks": missing_required_checks,
        "contractArtifacts": contract_artifacts,
        "missingArtifacts": missing_artifacts,
        "producerSide": producer_side,
        "consumerSide": consumer_side,
        "ready": not blocked_reasons,
        "blockedReasons": blocked_reasons,
        "proofStatus": _proof_status(
            boundary_sensitive=boundary_sensitive,
            missing_predicates=missing_predicates,
            proof_refs=proof_refs,
        ),
        "missingPredicates": missing_predicates,
        "proofRefs": proof_refs,
    }

def candidate_verification_state(root: Path, candidate: Candidate) -> dict[str, Any]:
    return build_verification_readiness(root, candidate)


def build_verification_report(
    root: Path,
    *,
    required_checks: Sequence[str],
    candidates: Sequence[Candidate],
) -> dict[str, Any]:
    candidate_states = [build_verification_readiness(root, candidate) for candidate in candidates]
    available_checks: set[str] = set()
    missing_required_checks: dict[str, None] = {}
    for candidate, state in zip(candidates, candidate_states, strict=False):
        available_checks_payload = cast(list[str], state["availableChecks"])
        available_checks.update(available_checks_payload)
        for check in cast(list[str], state["missingRequiredChecks"]):
            missing_required_checks.setdefault(check, None)
        if not candidate.required_checks:
            available_checks.update(_verification_capabilities(root))

    for check in required_checks:
        if check in _BOUNDARY_ENFORCED_CHECKS and check not in available_checks:
            missing_required_checks.setdefault(check, None)

    boundary_candidates = [
        state
        for candidate, state in zip(candidates, candidate_states, strict=False)
        if candidate.boundary_impact.cross_language or candidate.boundary_impact.impact_level != "none"
    ]
    missing_predicates = _ordered_unique(
        [
            *list(missing_required_checks),
            *[predicate for state in boundary_candidates for predicate in cast(list[str], state["missingPredicates"])],
        ]
    )
    proof_refs = _ordered_unique(
        [proof_ref for state in boundary_candidates for proof_ref in cast(list[str], state["proofRefs"])]
    )
    return {
        "requiredChecks": list(required_checks),
        "availableChecks": sorted(available_checks),
        "missingRequiredChecks": list(missing_required_checks),
        "boundaryCandidates": boundary_candidates,
        "proofStatus": _proof_status(
            boundary_sensitive=bool(boundary_candidates),
            missing_predicates=missing_predicates,
            proof_refs=proof_refs,
        ),
        "missingPredicates": missing_predicates,
        "proofRefs": proof_refs,
    }

def _proof_kind_from_ref(proof_ref: str) -> str:
    if proof_ref.startswith("artifact:"):
        return "boundary_contract"
    if proof_ref == "check:integration_test":
        return "boundary_integration"
    if proof_ref.startswith("check:"):
        check_name = proof_ref.split(":", 1)[1]
        if check_name in {"parse", "typecheck", "lint", "build", "unit_test", "integration_test"}:
            return check_name
    return "manual_review"


def _proof_records_from_report(report: dict[str, Any]) -> list[ProofRecord]:
    proof_status = cast(str, report["proofStatus"])
    if proof_status == "not_applicable":
        return []
    proof_refs = cast(list[str], report["proofRefs"])
    return [
        ProofRecord(
            proofId=f"verification-proof-{index + 1}",
            kind=cast(Any, _proof_kind_from_ref(proof_ref)),
            status=cast(Any, proof_status),
            predicate=proof_ref,
            references=[proof_ref],
        )
        for index, proof_ref in enumerate(proof_refs)
    ]

def _finalize_verification_report(report: dict[str, Any], checks: Sequence[VerificationCheckResult]) -> dict[str, Any]:
    failed_boundary_predicates: list[str] = []
    failed_check_kinds = {check.kind for check in checks if check.status == "failed"}
    if any(check.name == "boundary_contracts" and check.status == "failed" for check in checks):
        failed_boundary_predicates.append("boundary_contracts")
    if any(check.name == "boundary_integration" and check.status == "failed" for check in checks):
        failed_boundary_predicates.append("boundary_integration")

    proof_refs = cast(list[str], report["proofRefs"])
    finalized_proof_refs = [
        proof_ref
        for proof_ref in proof_refs
        if not proof_ref.startswith("check:") or proof_ref.split(":", 1)[1] not in failed_check_kinds
    ]
    missing_predicates = _ordered_unique(
        [
            *cast(list[str], report["missingPredicates"]),
            *failed_boundary_predicates,
            *[f"check:{kind}" for kind in sorted(failed_check_kinds & _BOUNDARY_ENFORCED_CHECKS)],
        ]
    )
    return {
        **report,
        "proofStatus": _proof_status(
            boundary_sensitive=bool(cast(list[dict[str, Any]], report["boundaryCandidates"])),
            missing_predicates=missing_predicates,
            proof_refs=finalized_proof_refs,
        ),
        "missingPredicates": missing_predicates,
        "proofRefs": finalized_proof_refs,
    }



def _verification_result_from_report(
    *,
    status: Literal["passed", "failed"],
    checks: list[VerificationCheckResult],
    report: dict[str, Any],
) -> VerificationResult:
    finalized_report = _finalize_verification_report(report, checks)
    missing_predicates = cast(list[str], finalized_report["missingPredicates"])
    proof_refs = cast(list[str], finalized_report["proofRefs"])
    return VerificationResult(
        status=status,
        checks=checks,
        readiness=VerificationReadiness(
            ready=status == "passed" and not missing_predicates,
            proofStatus=cast(Any, finalized_report["proofStatus"]),
            missingPredicates=missing_predicates,
            proofRefs=proof_refs,
        ),
        proofRecords=_proof_records_from_report(finalized_report),
    )


def build_verification_plan(
    root: Path,
    *,
    required_checks: Sequence[str],
    candidates: Sequence[Candidate],
) -> dict[str, Any]:
    return build_verification_report(root, required_checks=required_checks, candidates=candidates)


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
            markers = _openapi_contract_markers(content)
            if not markers:
                failures.append(f"{artifact} does not expose any OpenAPI path or operationId markers")
                continue
            evidence.append(f"validated OpenAPI boundary artifact marker: {artifact} ({len(markers)} contract markers)")
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

        contract_markers = _ordered_unique(
            marker for artifact in contract_artifacts for marker in _contract_markers(root, artifact)
        )
        producer_match = _first_contract_marker_match(root, producer_side, contract_markers) if contract_markers else None
        consumer_match = _first_contract_marker_match(root, consumer_side, contract_markers) if contract_markers else None
        if contract_markers and producer_match is None:
            failures.append(f"{candidate.id} producer-side files do not reference any contract markers from {', '.join(contract_artifacts)}")
            continue
        if contract_markers and consumer_match is None:
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
    for name, kind, choices in _SCRIPT_GROUPS:
        script_name = next((choice for choice in choices if choice in scripts), None)
        if script_name is None:
            continue
        checks.append(
            _run_command_check(root, name=name, kind=kind, command=[_npm_command(), "run", script_name])
        )
    return checks


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

    required: list[VerificationKind] = [cast(VerificationKind, check) for check in _ordered_unique(required_checks)]
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
        checks.extend(_python_toolchain_checks(root))

    adapters = detect_adapters(root)
    for adapter in adapters:
        checks.extend(adapter.verify(root))
    if any(adapter.metadata.language == "typescript" for adapter in adapters):
        checks.extend(_package_script_checks(root))

    checks.append(_verify_boundary_contracts(root))

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
    return _verification_result_from_report(status=status, checks=checks, report=verification_report)