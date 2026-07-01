from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from refactorq.core.candidate import Candidate
from refactorq.core.verification import VerificationCheckResult

from .base import AdapterMetadata, LanguageAdapter
from .python import PythonAdapter
from .typescript import TypeScriptAdapter


class _VerifiableAdapter(Protocol):
    def verify(self, root: Path) -> list[VerificationCheckResult]: ...


@dataclass(slots=True)
class RegisteredLanguageAdapter:
    raw: object
    metadata: AdapterMetadata
    name: str
    extensions: tuple[str, ...]

    def supports(self, root: Path) -> bool:
        return bool(getattr(self.raw, "supports")(root))

    def scan(self, root: Path) -> list[Candidate]:
        return cast(list[Candidate], getattr(self.raw, "scan")(root))

    def verify(self, root: Path) -> list[VerificationCheckResult]:
        if hasattr(self.raw, "verify"):
            return cast(_VerifiableAdapter, self.raw).verify(root)
        return []


def _metadata_for(adapter: object) -> AdapterMetadata:
    existing = getattr(adapter, "metadata", None)
    if isinstance(existing, AdapterMetadata):
        return existing

    name = cast(str, getattr(adapter, "name"))
    if name == "typescript":
        return AdapterMetadata(
            language="typescript",
            tier="worker",
            capabilities=["scan", "verify", "parse", "typecheck"],
            verificationChecks=["parse", "typecheck"],
        )
    if name == "python":
        return AdapterMetadata(
            language="python",
            tier="native",
            capabilities=["scan"],
            verificationChecks=[],
        )
    return AdapterMetadata(language=name)


def _register(adapter: object) -> RegisteredLanguageAdapter:
    return RegisteredLanguageAdapter(
        raw=adapter,
        metadata=_metadata_for(adapter),
        name=cast(str, getattr(adapter, "name")),
        extensions=tuple(cast(tuple[str, ...], getattr(adapter, "extensions"))),
    )


def available_adapters() -> list[LanguageAdapter]:
    return [_register(PythonAdapter()), _register(TypeScriptAdapter())]


def detect_adapters(root: Path) -> list[LanguageAdapter]:
    return [adapter for adapter in available_adapters() if adapter.supports(root)]
