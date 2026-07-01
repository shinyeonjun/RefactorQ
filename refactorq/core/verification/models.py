from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

VerificationStatus = Literal["passed", "failed", "skipped"]
VerificationKind = Literal["parse", "typecheck", "lint", "build", "unit_test", "integration_test"]
ProofStatus = Literal["proven", "missing", "disputed", "not_applicable"]
ProofKind = Literal[
    "parse",
    "typecheck",
    "lint",
    "build",
    "unit_test",
    "integration_test",
    "boundary_contract",
    "boundary_integration",
    "manual_review",
]


class VerificationCheckResult(BaseModel):
    name: str
    kind: VerificationKind
    status: VerificationStatus
    evidence: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)
    proof_ids: list[str] = Field(default_factory=list, alias="proofIds")


class ProofRecord(BaseModel):
    id: str = Field(alias="proofId")
    kind: ProofKind
    status: ProofStatus
    predicate: str
    owner: Literal["verifier"] = "verifier"
    evidence: list[str] = Field(default_factory=list)
    references: list[str] = Field(default_factory=list)


class VerificationReadiness(BaseModel):
    ready: bool = False
    proof_status: ProofStatus = Field(default="missing", alias="proofStatus")
    missing_predicates: list[str] = Field(default_factory=list, alias="missingPredicates")
    proof_refs: list[str] = Field(default_factory=list, alias="proofRefs")


class VerificationReport(BaseModel):
    status: Literal["passed", "failed"]
    checks: list[VerificationCheckResult] = Field(default_factory=list)
    readiness: VerificationReadiness = Field(default_factory=VerificationReadiness)
    proof_records: list[ProofRecord] = Field(default_factory=list, alias="proofRecords")


class VerificationResult(VerificationReport):
    pass


__all__ = [
    "ProofKind",
    "ProofRecord",
    "ProofStatus",
    "VerificationCheckResult",
    "VerificationKind",
    "VerificationReadiness",
    "VerificationReport",
    "VerificationResult",
    "VerificationStatus",
]
