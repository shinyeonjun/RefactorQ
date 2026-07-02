from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from refactorq.core.candidate import Candidate
from refactorq.core.verification import VerificationResult

from .models import BoundedPatchScope, GuardedApplyRequest, GuardedApplyResult, GuardedRepairRequest
from .prompts import build_apply_prompt, build_repair_prompt


_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "status": {"type": "string", "enum": ["applied", "no_change", "unsupported"]},
        "candidateIds": {"type": "array", "items": {"type": "string"}},
        "touchedFiles": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["status", "candidateIds", "touchedFiles", "summary"],
}

SUPPORTED_GUARDED_KINDS = frozenset({"duplicate_logic", "extract_function", "inline_function", "remove_abstraction"})
SUPPORTED_GUARDED_LANGUAGES = frozenset({"python", "typescript", "javascript"})
CODEX_EXEC_TIMEOUT_SECONDS = 120


class GuardedExecutionContractError(RuntimeError):
    pass


class CodexGuardedApplier:
    def is_available(self) -> bool:
        return shutil.which("codex") is not None

    def support_reason(self, root: Path, candidate: Candidate) -> str | None:
        if candidate.apply_mode_hint != "guarded":
            return "candidate is not marked for guarded handling"
        if not self.is_available():
            return "codex cli is not available"
        if candidate.kind not in SUPPORTED_GUARDED_KINDS:
            return "guarded Codex flow currently supports extract_function, inline_function, duplicate_logic, and remove_abstraction only"
        if candidate.language not in SUPPORTED_GUARDED_LANGUAGES:
            return "candidate language is not supported by guarded Codex flow"
        if len(candidate.files) != 1:
            return "candidate does not target a single file"
        target = root / candidate.files[0]
        if not target.exists():
            return "candidate target file is missing"
        if candidate.kind == "extract_function":
            if candidate.scope != "local":
                return "guarded Codex flow currently supports local-scope extract_function candidates only"
            if len(candidate.anchor_regions) != 1 or len(candidate.symbols) != 1:
                return "extract_function candidate does not target a single region and symbol"
            return None
        if candidate.kind == "inline_function":
            if candidate.scope not in {"local", "module"}:
                return "guarded Codex flow currently supports local or module inline_function candidates only"
            if len(candidate.anchor_regions) != 1 or len(candidate.symbols) != 1:
                return "inline_function candidate does not target a single region and symbol"
            return None
        if candidate.kind == "duplicate_logic":
            if candidate.scope not in {"local", "module"}:
                return "guarded Codex flow currently supports local or module duplicate_logic candidates only"
            if len(candidate.anchor_regions) < 2 or len(candidate.symbols) < 2:
                return "duplicate_logic candidate must target at least two regions and two symbols"
            return None
        if candidate.kind == "remove_abstraction":
            if candidate.scope not in {"local", "module"}:
                return "guarded Codex flow currently supports local or module remove_abstraction candidates only"
            if len(candidate.anchor_regions) != 1 or len(candidate.symbols) != 1:
                return "remove_abstraction candidate does not target a single region and symbol"
            return None
        return "candidate kind is not supported by guarded Codex flow"

    def build_apply_request(self, candidate: Candidate) -> GuardedApplyRequest:
        return GuardedApplyRequest(
            scope=BoundedPatchScope(
                candidateIds=[candidate.id],
                allowedFiles=list(candidate.files),
                anchorRegions=list(candidate.anchor_regions),
                requiredChecks=list(candidate.required_checks),
            ),
            candidate=candidate,
        )

    def build_repair_request(
        self,
        candidates: list[Candidate],
        verification: VerificationResult,
    ) -> GuardedRepairRequest:
        unique_files = sorted({file for candidate in candidates for file in candidate.files})
        anchor_regions = [region for candidate in candidates for region in candidate.anchor_regions]
        required_checks = sorted({check for candidate in candidates for check in candidate.required_checks})
        return GuardedRepairRequest(
            scope=BoundedPatchScope(
                candidateIds=[candidate.id for candidate in candidates],
                allowedFiles=unique_files,
                anchorRegions=anchor_regions,
                requiredChecks=required_checks,
            ),
            candidates=list(candidates),
            verification=verification,
        )


    def apply(self, root: Path, candidate: Candidate) -> GuardedApplyResult:
        support_reason = self.support_reason(root, candidate)
        if support_reason is not None:
            return GuardedApplyResult(status="unsupported", summary=[support_reason], details={"reason": support_reason})
        request = self.build_apply_request(candidate)
        prompt = build_apply_prompt(request)
        return self._run(root, prompt, request.scope.allowed_files)

    def repair(self, root: Path, candidates: list[Candidate], verification: VerificationResult) -> GuardedApplyResult:
        if not candidates:
            return GuardedApplyResult(status="unsupported", summary=["no guarded candidates available for repair"], details={})
        if not self.is_available():
            return GuardedApplyResult(status="unsupported", summary=["codex cli is not available"], details={})
        request = self.build_repair_request(candidates, verification)
        prompt = build_repair_prompt(request)
        return self._run(root, prompt, request.scope.allowed_files)


    def _stage_workspace(self, root: Path, temp_root: Path, allowed_files: list[str]) -> dict[str, bytes | None]:
        original_files: dict[str, bytes | None] = {}
        for rel_path in allowed_files:
            source = root / rel_path
            if not source.exists():
                original_files[rel_path] = None
                continue
            content = source.read_bytes()
            original_files[rel_path] = content
            target = temp_root / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        return original_files

    def _sync_workspace(
        self,
        root: Path,
        temp_root: Path,
        allowed_files: list[str],
        original_files: dict[str, bytes | None],
    ) -> None:
        for rel_path in allowed_files:
            original = original_files.get(rel_path)
            staged = temp_root / rel_path
            target = root / rel_path
            if staged.exists():
                staged_bytes = staged.read_bytes()
                if original != staged_bytes:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(staged_bytes)
                continue
            if original is not None and target.exists():
                target.unlink()

    def _run(self, root: Path, prompt: str, allowed_files: list[str]) -> GuardedApplyResult:
        with tempfile.TemporaryDirectory(prefix="refactorq-codex-") as temp_dir:
            session_root = Path(temp_dir)
            temp_root = session_root / "workspace"
            temp_root.mkdir(parents=True, exist_ok=True)
            original_files = self._stage_workspace(root, temp_root, allowed_files)
            schema_path = session_root / "codex-output-schema.json"
            output_path = session_root / "codex-output.json"
            schema_path.write_text(json.dumps(_OUTPUT_SCHEMA), encoding="utf-8")
            subprocess.run(
                [
                    "codex",
                    "exec",
                    "--skip-git-repo-check",
                    "--sandbox",
                    "workspace-write",
                    "--cd",
                    str(temp_root),
                    "--output-schema",
                    str(schema_path),
                    "--output-last-message",
                    str(output_path),
                    "--color",
                    "never",
                    "--ephemeral",
                    "--ignore-user-config",
                    "-",
                ],
                input=prompt,
                text=True,
                encoding="utf-8",
                capture_output=True,
                check=True,
                timeout=CODEX_EXEC_TIMEOUT_SECONDS,
            )
            try:
                payload_text = output_path.read_text(encoding="utf-8")
            except OSError as exc:
                raise GuardedExecutionContractError("Codex guarded execution did not produce structured output") from exc
            payload = json.loads(payload_text)
            result = GuardedApplyResult.model_validate(payload)
            self._sync_workspace(root, temp_root, allowed_files, original_files)
            return result
