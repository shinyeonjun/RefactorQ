from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, TypeAdapter

PROTOCOL_VERSION = 1
PROTOCOL_CAPABILITIES = ["scan", "deterministic-ordering", "typescript-semantic-candidates"]


class WorkerScanRequest(BaseModel):
    protocol_version: int = Field(alias="protocolVersion")
    capabilities: list[str] = Field(default_factory=lambda: ["scan"])
    command: Literal["scan"]
    root: str


class WorkerProtocolError(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class WorkerScanSuccess(BaseModel):
    protocol_version: int = Field(alias="protocolVersion")
    capabilities: list[str] = Field(default_factory=lambda: PROTOCOL_CAPABILITIES.copy())
    ok: Literal[True]
    candidates: list[dict[str, Any]] = Field(default_factory=list)


class WorkerScanFailure(BaseModel):
    protocol_version: int = Field(alias="protocolVersion")
    capabilities: list[str] = Field(default_factory=lambda: PROTOCOL_CAPABILITIES.copy())
    ok: Literal[False]
    error: WorkerProtocolError


WorkerScanResponse = WorkerScanSuccess | WorkerScanFailure
WORKER_SCAN_RESPONSE_ADAPTER: TypeAdapter[WorkerScanResponse] = TypeAdapter(WorkerScanResponse)


__all__ = [
    "PROTOCOL_CAPABILITIES",
    "PROTOCOL_VERSION",
    "WORKER_SCAN_RESPONSE_ADAPTER",
    "WorkerProtocolError",
    "WorkerScanFailure",
    "WorkerScanRequest",
    "WorkerScanResponse",
    "WorkerScanSuccess",
]
