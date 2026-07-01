from __future__ import annotations

from pathlib import PurePosixPath

from refactorq.core.candidate.models import BoundaryImpact, BoundaryType, Candidate, Provenance
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
_CONTRACT_PRESERVING_KINDS = {"extract_function", "duplicate_logic", "remove_abstraction"}


def _is_low_impact_contract_preserving_candidate(candidate: Candidate) -> bool:
    return (
        candidate.kind in _CONTRACT_PRESERVING_KINDS
        and len(candidate.files) == 1
        and candidate.scope in {"local", "module"}
    )


def enrich_boundary_candidates(repo: RepoSnapshot, candidates: list[Candidate]) -> list[Candidate]:
    enriched = [_enrich_candidate(repo, candidate) for candidate in candidates]
    enriched.extend(_build_boundary_review_candidates(repo))
    return enriched


def _build_boundary_review_candidates(repo: RepoSnapshot) -> list[Candidate]:
    candidates: list[Candidate] = []
    for artifact in repo.boundary_artifacts:
        boundary_types = _artifact_boundary_types(artifact)
        if not boundary_types:
            continue
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
                    "requiredChecks": ["integration_test"],
                    "provenance": {
                        "detectors": ["repo-boundary-artifact"],
                        "evidence": [f"artifact:{artifact}", *[f"type:{boundary_type}" for boundary_type in boundary_types]],
                    },
                }
            )
        )
    return candidates


def _enrich_candidate(repo: RepoSnapshot, candidate: Candidate) -> Candidate:
    boundary_types = set(candidate.boundary_impact.boundary_types)
    producer_side = list(candidate.boundary_impact.producer_side)
    consumer_side = list(candidate.boundary_impact.consumer_side)
    contract_artifacts = list(candidate.boundary_impact.contract_artifacts)

    touched_boundary_surface = False
    for artifact in repo.boundary_artifacts:
        artifact_types = _artifact_boundary_types(artifact)
        if artifact_types:
            contract_artifacts.append(artifact)
        for boundary_type in artifact_types:
            boundary_types.add(boundary_type)

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

    cross_language = candidate.boundary_impact.cross_language or (
        repo.mixed_language and touched_boundary_surface and bool(boundary_types)
    )
    impact_level = candidate.boundary_impact.impact_level
    if cross_language and impact_level == "none":
        if candidate.apply_mode_hint == "auto" or _is_low_impact_contract_preserving_candidate(candidate):
            impact_level = "low"
        else:
            impact_level = "medium"

    normalized_contract_artifacts = sorted(set(contract_artifacts)) if cross_language else []
    normalized_boundary_types = sorted(boundary_types)
    normalized_producer_side = sorted(set(producer_side))
    normalized_consumer_side = sorted(set(consumer_side))

    if candidate.language == "python" and cross_language and not normalized_producer_side:
        normalized_producer_side = list(candidate.files)
    if candidate.language in {"typescript", "javascript"} and cross_language and not normalized_consumer_side:
        normalized_consumer_side = list(candidate.files)

    if (
        cross_language == candidate.boundary_impact.cross_language
        and normalized_boundary_types == candidate.boundary_impact.boundary_types
        and normalized_contract_artifacts == candidate.boundary_impact.contract_artifacts
        and impact_level == candidate.boundary_impact.impact_level
        and normalized_producer_side == candidate.boundary_impact.producer_side
        and normalized_consumer_side == candidate.boundary_impact.consumer_side
    ):
        return candidate

    provenance = Provenance(
        detectors=[*candidate.provenance.detectors, "boundary-mixed-language-heuristic"],
        evidence=[
            *candidate.provenance.evidence,
            f"mixed_language:{repo.mixed_language}",
            *[f"boundary_artifact:{artifact}" for artifact in normalized_contract_artifacts],
            *[f"boundary_type:{boundary_type}" for boundary_type in normalized_boundary_types],
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
    return candidate.model_copy(update={"boundary_impact": boundary_impact, "provenance": provenance})


def _artifact_boundary_types(path: str) -> tuple[BoundaryType, ...]:
    return _ARTIFACT_BOUNDARY_TYPES.get(PurePosixPath(path).name.lower(), ())
