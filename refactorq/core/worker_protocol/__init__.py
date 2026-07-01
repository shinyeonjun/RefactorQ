from __future__ import annotations

from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, Field, TypeAdapter

PROTOCOL_VERSION = 1
PROTOCOL_CAPABILITIES = [
    "scan",
    "verify",
    "deterministic-ordering",
    "typescript-semantic-candidates",
]

WorkerCommand: TypeAlias = Literal["scan", "verify"]


class WorkerProtocolRequest(BaseModel):
    protocol_version: int = Field(alias="protocolVersion")
    capabilities: list[str] = Field(default_factory=lambda: ["scan"])
    command: WorkerCommand
    root: str


class WorkerScanRequest(WorkerProtocolRequest):
    command: Literal["scan"]
    capabilities: list[str] = Field(default_factory=lambda: ["scan"])


class WorkerVerifyRequest(WorkerProtocolRequest):
    command: Literal["verify"]
    capabilities: list[str] = Field(default_factory=lambda: ["verify"])


class WorkerProtocolError(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class WorkerVerificationCheckPayload(BaseModel):
    name: str
    kind: Literal["parse", "typecheck", "lint", "build", "unit_test"]
    status: Literal["passed", "failed", "skipped"]
    evidence: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)


class WorkerScanSuccess(BaseModel):
    protocol_version: int = Field(alias="protocolVersion")
    capabilities: list[str] = Field(default_factory=lambda: PROTOCOL_CAPABILITIES.copy())
    ok: Literal[True]
    command: Literal["scan"]
    candidates: list[dict[str, Any]] = Field(default_factory=list)


class WorkerVerifySuccess(BaseModel):
    protocol_version: int = Field(alias="protocolVersion")
    capabilities: list[str] = Field(default_factory=lambda: PROTOCOL_CAPABILITIES.copy())
    ok: Literal[True]
    command: Literal["verify"]
    checks: list[WorkerVerificationCheckPayload] = Field(default_factory=list)


class WorkerFailure(BaseModel):
    protocol_version: int = Field(alias="protocolVersion")
    capabilities: list[str] = Field(default_factory=lambda: PROTOCOL_CAPABILITIES.copy())
    ok: Literal[False]
    command: WorkerCommand
    error: WorkerProtocolError


WorkerScanResponse = WorkerScanSuccess | WorkerFailure
WorkerVerifyResponse = WorkerVerifySuccess | WorkerFailure
WORKER_SCAN_RESPONSE_ADAPTER: TypeAdapter[WorkerScanResponse] = TypeAdapter(WorkerScanResponse)
WORKER_VERIFY_RESPONSE_ADAPTER: TypeAdapter[WorkerVerifyResponse] = TypeAdapter(WorkerVerifyResponse)


__all__ = [
    "PROTOCOL_CAPABILITIES",
    "PROTOCOL_VERSION",
    "WORKER_SCAN_RESPONSE_ADAPTER",
    "WORKER_VERIFY_RESPONSE_ADAPTER",
    "WorkerFailure",
    "WorkerProtocolError",
    "WorkerProtocolRequest",
    "WorkerScanRequest",
    "WorkerScanResponse",
    "WorkerScanSuccess",
    "WorkerVerificationCheckPayload",
    "WorkerVerifyRequest",
    "WorkerVerifyResponse",
    "WorkerVerifySuccess",
]
