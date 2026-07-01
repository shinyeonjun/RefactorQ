from __future__ import annotations

from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from refactorq.core.candidate import Candidate
from refactorq.core.verification import VerificationCheckResult

AdapterCapability = Literal[
    "scan",
    "verify",
    "parse",
    "lint",
    "typecheck",
    "build",
    "unit_test",
    "integration_test",
    "boundary_contracts",
]
AdapterTier = Literal["native", "worker", "bridge"]


class AdapterMetadata(BaseModel):
    language: str
    tier: AdapterTier = "native"
    capabilities: list[AdapterCapability] = Field(default_factory=list)
    verification_checks: list[str] = Field(default_factory=list, alias="verificationChecks")


class LanguageAdapter(Protocol):
    name: str
    extensions: tuple[str, ...]
    metadata: AdapterMetadata

    def supports(self, root: Path) -> bool: ...

    def scan(self, root: Path) -> list[Candidate]: ...

    def verify(self, root: Path) -> list[VerificationCheckResult]: ...
