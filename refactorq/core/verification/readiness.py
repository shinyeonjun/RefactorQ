from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Sequence, cast

from refactorq.adapters.registry import detect_adapters
from refactorq.core.candidate.models import Candidate, VerificationCheck
from refactorq.core.filesystem import walk_source_files
from refactorq.core.repo import detect_repo

from .command_checks import SCRIPT_GROUPS, package_scripts


_SOURCE_EXTENSIONS = (".py", ".ts", ".tsx", ".js", ".jsx")
_PRODUCER_TOKENS = {"api", "apis", "route", "routes", "controller", "controllers", "backend", "server"}
_CONSUMER_TOKENS = {"client", "clients", "frontend", "web", "sdk", "ui"}
_BOUNDARY_ENFORCED_CHECKS = {"build", "integration_test"}


def ordered_unique(items: Iterable[str]) -> list[str]:
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

    scripts = package_scripts(root)
    for _, kind, choices in SCRIPT_GROUPS:
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
    return ordered_unique(producers), ordered_unique(consumers)


def _existing_relative_paths(root: Path, rel_paths: Iterable[str]) -> list[str]:
    existing: list[str] = []
    for rel_path in ordered_unique(rel_paths):
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
    contract_artifacts = ordered_unique(candidate.boundary_impact.contract_artifacts)
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
        blocked_reasons.append("required verification checks are not available: " + ", ".join(missing_required_checks))

    missing_predicates = ordered_unique(
        [
            *missing_required_checks,
            *[f"artifact:{artifact}" for artifact in missing_artifacts],
            *(["contract_artifacts"] if candidate.boundary_impact.cross_language and not contract_artifacts else []),
            *(["producer_side"] if "integration_test" in candidate.required_checks and not producer_side else []),
            *(["consumer_side"] if "integration_test" in candidate.required_checks and not consumer_side else []),
        ]
    )
    proof_refs = ordered_unique(
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
    missing_predicates = ordered_unique(
        [
            *list(missing_required_checks),
            *[predicate for state in boundary_candidates for predicate in cast(list[str], state["missingPredicates"])],
        ]
    )
    proof_refs = ordered_unique(
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


def build_verification_plan(
    root: Path,
    *,
    required_checks: Sequence[str],
    candidates: Sequence[Candidate],
) -> dict[str, Any]:
    return build_verification_report(root, required_checks=required_checks, candidates=candidates)
