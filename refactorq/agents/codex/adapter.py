from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from refactorq.core.candidate import Candidate

from .models import GuardedApplyResult

_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "status": {"type": "string", "enum": ["applied", "no_change", "unsupported"]},
        "touchedFiles": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "array", "items": {"type": "string"}},
        "details": {
            "type": "object",
            "additionalProperties": {"type": "string"},
        },
    },
    "required": ["status", "touchedFiles", "summary", "details"],
}

_SUPPORTED_KINDS = {"extract_function"}
_SUPPORTED_LANGUAGES = {"python", "typescript", "javascript"}


class CodexGuardedApplier:
    def is_available(self) -> bool:
        return shutil.which("codex") is not None

    def support_reason(self, root: Path, candidate: Candidate) -> str | None:
        if candidate.apply_mode_hint != "guarded":
            return "candidate is not marked for guarded handling"
        if not self.is_available():
            return "codex cli is not available"
        if candidate.kind not in _SUPPORTED_KINDS:
            return "guarded Codex flow currently supports extract_function only"
        if candidate.language not in _SUPPORTED_LANGUAGES:
            return "candidate language is not supported by guarded Codex flow"
        if candidate.scope != "local":
            return "guarded Codex flow currently supports local-scope candidates only"
        if len(candidate.files) != 1 or len(candidate.anchor_regions) != 1 or len(candidate.symbols) != 1:
            return "candidate does not target a single file, region, and symbol"
        target = root / candidate.files[0]
        if not target.exists():
            return "candidate target file is missing"
        return None

    def apply(self, root: Path, candidate: Candidate) -> GuardedApplyResult:
        support_reason = self.support_reason(root, candidate)
        if support_reason is not None:
            return GuardedApplyResult(status="unsupported", summary=[support_reason], details={"reason": support_reason})

        with tempfile.TemporaryDirectory(prefix="refactorq-codex-") as temp_dir:
            temp_root = Path(temp_dir)
            schema_path = temp_root / "codex-output-schema.json"
            output_path = temp_root / "codex-output.json"
            schema_path.write_text(json.dumps(_OUTPUT_SCHEMA), encoding="utf-8")
            prompt = self._build_prompt(candidate)
            subprocess.run(
                [
                    "codex",
                    "exec",
                    "--skip-git-repo-check",
                    "--sandbox",
                    "workspace-write",
                    "--cd",
                    str(root),
                    "--output-schema",
                    str(schema_path),
                    "--output-last-message",
                    str(output_path),
                    "--color",
                    "never",
                    "--ephemeral",
                    "-",
                ],
                input=prompt,
                text=True,
                encoding="utf-8",
                capture_output=True,
                check=True,
            )
            payload = json.loads(output_path.read_text(encoding="utf-8"))
        return GuardedApplyResult.model_validate(payload)

    def _build_prompt(self, candidate: Candidate) -> str:
        region = candidate.anchor_regions[0]
        required_checks = ", ".join(candidate.required_checks) if candidate.required_checks else "none"
        return (
            "You are applying one guarded refactoring candidate inside an existing repository.\n"
            "Modify only the allowed file. Preserve behavior and public interfaces.\n"
            "Do not touch tests, docs, configs, or any other files.\n"
            "If you cannot complete the candidate safely, make no changes and return status no_change.\n\n"
            f"Candidate ID: {candidate.id}\n"
            f"Kind: {candidate.kind}\n"
            f"Language: {candidate.language}\n"
            f"File: {candidate.files[0]}\n"
            f"Symbol: {candidate.symbols[0]}\n"
            f"Region: lines {region.start_line}-{region.end_line}\n"
            f"Title: {candidate.title}\n"
            f"Description: {candidate.description}\n"
            f"Required checks: {required_checks}\n\n"
            "Preferred implementation: extract a small private helper or equivalent local refactor so the"
            " target function becomes shorter and clearer without changing behavior.\n\n"
            "Return JSON matching the provided schema with touchedFiles, summary, and details.\n"
        )
