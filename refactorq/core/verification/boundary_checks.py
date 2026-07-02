from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Sequence

from refactorq.core.repo import detect_repo

from .models import VerificationCheckResult


def _ordered_unique(items: Iterable[str]) -> list[str]:
    ordered: dict[str, None] = {}
    for item in items:
        ordered.setdefault(item, None)
    return list(ordered)


def openapi_contract_markers(content: str) -> list[str]:
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


def contract_markers(root: Path, artifact: str) -> list[str]:
    path = root / artifact
    if not path.exists():
        return []
    content = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if path.name in {"openapi.yaml", "openapi.yml"}:
        return openapi_contract_markers(content)
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


def first_contract_marker_match(
    root: Path, rel_paths: Sequence[str], markers: Sequence[str]
) -> tuple[str, str] | None:
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


def verify_boundary_contracts(root: Path) -> VerificationCheckResult:
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
            markers = openapi_contract_markers(content)
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
