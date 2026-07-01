from __future__ import annotations

import json
import subprocess
from pathlib import Path

from pydantic import ValidationError

from refactorq.core.candidate.models import Candidate, Provenance
from refactorq.core.filesystem import walk_source_files
from refactorq.core.worker_protocol import (
    PROTOCOL_VERSION,
    WORKER_SCAN_RESPONSE_ADAPTER,
    WorkerScanRequest,
)

WORKER_TIMEOUT_SECONDS = 20
SUPPORTED_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx")


class TypeScriptAdapter:
    name: str = "typescript"
    extensions: tuple[str, ...] = SUPPORTED_EXTENSIONS

    def supports(self, root: Path) -> bool:
        return any(True for _ in walk_source_files(root, self.extensions))

    def scan(self, root: Path) -> list[Candidate]:
        try:
            payload = self._invoke_worker(root)
            response = WORKER_SCAN_RESPONSE_ADAPTER.validate_python(payload)
        except subprocess.TimeoutExpired:
            return [self._failure_candidate(root, code="timeout", message="TypeScript worker timed out")]
        except FileNotFoundError as exc:
            return [
                self._failure_candidate(
                    root,
                    code="worker_not_found",
                    message="TypeScript worker executable was not found",
                    details={"executable": exc.filename or "node"},
                )
            ]
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr
            stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout
            return [
                self._failure_candidate(
                    root,
                    code="non_zero_exit",
                    message="TypeScript worker exited with a non-zero status",
                    details={
                        "returncode": exc.returncode,
                        "stderr": (stderr or "")[:500],
                        "stdout": (stdout or "")[:500],
                    },
                )
            ]
        except json.JSONDecodeError as exc:
            return [
                self._failure_candidate(
                    root,
                    code="malformed_json",
                    message="TypeScript worker returned malformed JSON",
                    details={"error": str(exc)},
                )
            ]
        except ValidationError as exc:
            return [
                self._failure_candidate(
                    root,
                    code="invalid_response",
                    message="TypeScript worker returned an invalid protocol payload",
                    details={"error": str(exc)},
                )
            ]

        if response.protocol_version != PROTOCOL_VERSION:
            return [
                self._failure_candidate(
                    root,
                    code="version_mismatch",
                    message="TypeScript worker protocol version mismatch",
                    details={
                        "expected": PROTOCOL_VERSION,
                        "actual": response.protocol_version,
                    },
                )
            ]

        if not response.ok:
            return [
                self._failure_candidate(
                    root,
                    code=response.error.code,
                    message=response.error.message,
                    details=response.error.details,
                )
            ]

        try:
            candidates = [Candidate.model_validate(candidate) for candidate in response.candidates]
        except ValidationError as exc:
            return [
                self._failure_candidate(
                    root,
                    code="invalid_candidate",
                    message="TypeScript worker returned an invalid candidate payload",
                    details={"error": str(exc)},
                )
            ]

        return candidates

    def _invoke_worker(self, root: Path) -> object:
        request = WorkerScanRequest(protocolVersion=PROTOCOL_VERSION, command="scan", root=str(root.resolve()))
        completed = subprocess.run(
            self._worker_command(),
            input=request.model_dump_json(by_alias=True),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=WORKER_TIMEOUT_SECONDS,
            check=True,
        )
        return json.loads(completed.stdout)

    def _worker_command(self) -> list[str]:
        worker_root = Path(__file__).resolve().parents[3] / "workers" / "ts-adapter"
        built_worker = worker_root / "dist" / "index.js"
        if built_worker.exists():
            return ["node", str(built_worker)]
        return ["node", "--experimental-strip-types", str(worker_root / "src" / "index.ts")]

    def _failure_candidate(
        self,
        root: Path,
        *,
        code: str,
        message: str,
        details: dict[str, object] | None = None,
    ) -> Candidate:
        root_name = root.resolve().name or "."
        evidence = [f"code:{code}", f"root:{root_name}"]
        if details:
            evidence.extend(f"{key}:{value}" for key, value in sorted(details.items()))
        return Candidate(
            id=f"ts-worker-bridge-{code}-{root_name}",
            kind="custom",
            title="Review TypeScript worker bridge failure",
            description=f"Transitional TypeScript worker bridge failed: {message}",
            language="typescript",
            scope="package",
            source=["agent"],
            files=[],
            confidence=0.0,
            applyModeHint="report_only",
            provenance=Provenance(detectors=["ts-worker-bridge"], evidence=evidence),
        )
