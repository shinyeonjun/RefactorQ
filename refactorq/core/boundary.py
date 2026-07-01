from __future__ import annotations

from pathlib import Path, PurePosixPath

from refactorq.core.candidate.models import BoundaryImpact, BoundaryType, Candidate, Provenance, VerificationCheck
from refactorq.core.filesystem import walk_source_files
from refactorq.core.repo import RepoSnapshot

_ARTIFACT_BOUNDARY_TYPES: dict[str, tuple[BoundaryType, ...]] = {
    "openapi.yaml": ("openapi", "http_api"),
    "openapi.yml": ("openapi", "http_api"),
    "swagger.json": ("openapi", "http_api"),
    "schema.json": ("json_schema",),
    ".env.example": ("env", "config"),
}

_PATH_TOKEN_BOUNDARY_TYPES: dict[str, BoundaryType] = {
    "api": "http_api",
    "apis": "http_api",
    "route": "http_api",
    "routes": "http_api",
    "controller": "http_api",
    "controllers": "http_api",
    "client": "generated_client",
    "clients": "generated_client",
    "frontend": "http_api",
    "backend": "http_api",
    "server": "http_api",
    "graphql": "graphql",
    "schema": "json_schema",
    "schemas": "json_schema",
    "config": "config",
    "configs": "config",
    "settings": "config",
    "env": "env",
}

_PRODUCER_TOKENS = {"api", "apis", "route", "routes", "controller", "controllers", "backend", "server"}
_CONSUMER_TOKENS = {"client", "clients", "frontend", "web", "sdk"}
_CONTRACT_PRESERVING_KINDS = {"extract_function", "inline_function", "duplicate_logic", "remove_abstraction"}
_BOUNDARY_BUILD_TYPES = {"http_api", "generated_client", "graphql", "json_schema", "openapi"}


def _boundary_required_checks(
    *,
    cross_language: bool,
    boundary_types: set[BoundaryType],
    impact_level: str,
) -> list[VerificationCheck]:
    required_checks: list[VerificationCheck] = []
    if cross_language:
        required_checks.append("integration_test")
    if boundary_types & _BOUNDARY_BUILD_TYPES:
        required_checks.append("build")
    if impact_level in {"medium", "high"}:
        required_checks.append("unit_test")
    return required_checks


def _is_low_impact_contract_preserving_candidate(candidate: Candidate) -> bool:
    return (
        candidate.kind in _CONTRACT_PRESERVING_KINDS
        and len(candidate.files) == 1
        and candidate.scope in {"local", "module"}
    )


def _repo_boundary_side_hints(root: Path | None) -> tuple[list[str], list[str]]:
    if root is None:
        return [], []
    producer_side: list[str] = []
    consumer_side: list[str] = []
    for path in walk_source_files(root, (".py", ".ts", ".tsx", ".js", ".jsx")):
        rel_path = path.relative_to(root).as_posix()
        tokens = {part.lower() for part in PurePosixPath(rel_path).parts}
        file_stem = PurePosixPath(rel_path).stem.lower()
        if file_stem in _PATH_TOKEN_BOUNDARY_TYPES:
            tokens.add(file_stem)
        if tokens & _PRODUCER_TOKENS:
            producer_side.append(rel_path)
        if tokens & _CONSUMER_TOKENS:
            consumer_side.append(rel_path)
    return sorted(set(producer_side)), sorted(set(consumer_side))



def enrich_boundary_candidates(repo: RepoSnapshot, candidates: list[Candidate], root: Path | None = None) -> list[Candidate]:
    repo_producer_hints, repo_consumer_hints = _repo_boundary_side_hints(root)
    enriched = [_enrich_candidate(repo, candidate, repo_producer_hints, repo_consumer_hints) for candidate in candidates]
    enriched.extend(_build_boundary_review_candidates(repo))
    return enriched


def _build_boundary_review_candidates(repo: RepoSnapshot) -> list[Candidate]:
    candidates: list[Candidate] = []
    for artifact in repo.boundary_artifacts:
        boundary_types = _artifact_boundary_types(artifact)
        if not boundary_types:
            continue
        required_checks = _boundary_required_checks(
            cross_language=repo.mixed_language,
            boundary_types=set(boundary_types),
            impact_level="high" if repo.mixed_language else "medium",
        )
        candidates.append(
            Candidate.model_validate(
                {
                    "id": f"boundary-review-{artifact.replace('/', '-').replace('.', '-')}",
                    "kind": "custom",
                    "title": f"Review boundary contract artifact {artifact}",
                    "description": (
                        f"Boundary artifact `{artifact}` coordinates cross-language contracts and should"
                        " stay aligned before structural refactors land."
                    ),
                    "language": "mixed" if repo.mixed_language else "unknown",
                    "scope": "architecture",
                    "source": ["graph"],
                    "files": [artifact],
                    "estimatedBenefit": {"maintainabilityGain": 0.22},
                    "estimatedRisk": {
                        "semanticRisk": 0.25,
                        "apiRisk": 0.65,
                        "testRisk": 0.35,
                        "conflictRisk": 0.1,
                    },
                    "estimatedDiff": {"filesTouched": 1, "linesModified": 1},
                    "boundaryImpact": {
                        "crossLanguage": repo.mixed_language,
                        "boundaryTypes": list(boundary_types),
                        "contractArtifacts": [artifact],
                        "impactLevel": "high" if repo.mixed_language else "medium",
                    },
                    "confidence": 0.68,
                    "applyModeHint": "report_only",
                    "requiredChecks": required_checks,
                    "provenance": {
                        "detectors": ["repo-boundary-artifact"],
                        "evidence": [f"artifact:{artifact}", *[f"type:{boundary_type}" for boundary_type in boundary_types]],
                    },
                }
            )
        )
    return candidates


def _enrich_candidate(
    repo: RepoSnapshot,
    candidate: Candidate,
    repo_producer_hints: list[str],
    repo_consumer_hints: list[str],
) -> Candidate:
    boundary_types = set(candidate.boundary_impact.boundary_types)
    producer_side = list(candidate.boundary_impact.producer_side)
    consumer_side = list(candidate.boundary_impact.consumer_side)
    linked_contract_artifacts = set(candidate.boundary_impact.contract_artifacts)

    touched_boundary_surface = False
    for rel_path in candidate.files:
        path = PurePosixPath(rel_path)
        tokens = {part.lower() for part in path.parts}
        file_stem = path.stem.lower()
        if file_stem in _PATH_TOKEN_BOUNDARY_TYPES:
            tokens.add(file_stem)
        matched = {token for token in tokens if token in _PATH_TOKEN_BOUNDARY_TYPES}
        if not matched:
            continue
        touched_boundary_surface = True
        for token in matched:
            boundary_types.add(_PATH_TOKEN_BOUNDARY_TYPES[token])
        if matched & _PRODUCER_TOKENS:
            producer_side.append(rel_path)
        if matched & _CONSUMER_TOKENS:
            consumer_side.append(rel_path)

    for artifact in repo.boundary_artifacts:
        artifact_types = _artifact_boundary_types(artifact)
        if not artifact_types:
            continue
        if artifact in linked_contract_artifacts or boundary_types.intersection(artifact_types):
            linked_contract_artifacts.add(artifact)
            boundary_types.update(artifact_types)

    cross_language = candidate.boundary_impact.cross_language or (
        repo.mixed_language and touched_boundary_surface and bool(boundary_types)
    )
    impact_level = candidate.boundary_impact.impact_level
    if cross_language and impact_level == "none":
        if candidate.apply_mode_hint == "auto" or _is_low_impact_contract_preserving_candidate(candidate):
            impact_level = "low"
        else:
            impact_level = "medium"

    normalized_contract_artifacts = sorted(linked_contract_artifacts) if cross_language else []
    normalized_boundary_types = sorted(boundary_types)
    normalized_producer_side = sorted(set(producer_side))
    normalized_consumer_side = sorted(set(consumer_side))

    if candidate.language == "python" and cross_language and not normalized_producer_side:
        normalized_producer_side = list(candidate.files)
    if candidate.language in {"typescript", "javascript"} and cross_language and not normalized_consumer_side:
        normalized_consumer_side = list(candidate.files)
    if cross_language and not normalized_producer_side:
        normalized_producer_side = [path for path in repo_producer_hints if path not in normalized_consumer_side]
    if cross_language and not normalized_consumer_side:
        normalized_consumer_side = [path for path in repo_consumer_hints if path not in normalized_producer_side]
    required_checks = list(candidate.required_checks)
    for check in _boundary_required_checks(
        cross_language=cross_language,
        boundary_types=boundary_types,
        impact_level=impact_level,
    ):
        if check not in required_checks:
            required_checks.append(check)

    added_required_checks = [check for check in required_checks if check not in candidate.required_checks]
    if (
        cross_language == candidate.boundary_impact.cross_language
        and normalized_boundary_types == candidate.boundary_impact.boundary_types
        and normalized_contract_artifacts == candidate.boundary_impact.contract_artifacts
        and impact_level == candidate.boundary_impact.impact_level
        and normalized_producer_side == candidate.boundary_impact.producer_side
        and normalized_consumer_side == candidate.boundary_impact.consumer_side
        and required_checks == candidate.required_checks
    ):
        return candidate

    provenance = Provenance(
        detectors=[*candidate.provenance.detectors, "boundary-mixed-language-heuristic"],
        evidence=[
            *candidate.provenance.evidence,
            f"mixed_language:{repo.mixed_language}",
            *[f"boundary_artifact:{artifact}" for artifact in normalized_contract_artifacts],
            *[f"boundary_type:{boundary_type}" for boundary_type in normalized_boundary_types],
            *[f"required_check:{check}" for check in added_required_checks],
        ],
    )
    boundary_impact = BoundaryImpact(
        crossLanguage=cross_language,
        boundaryTypes=normalized_boundary_types,
        producerSide=normalized_producer_side,
        consumerSide=normalized_consumer_side,
        contractArtifacts=normalized_contract_artifacts,
        impactLevel=impact_level,
    )
    return candidate.model_copy(update={"boundary_impact": boundary_impact, "provenance": provenance, "required_checks": required_checks})


def _artifact_boundary_types(path: str) -> tuple[BoundaryType, ...]:
    return _ARTIFACT_BOUNDARY_TYPES.get(PurePosixPath(path).name.lower(), ())
