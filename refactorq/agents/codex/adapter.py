from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from refactorq.core.candidate import Candidate
from refactorq.core.verification import VerificationResult

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
        prompt = self._build_apply_prompt(candidate)
        return self._run(root, prompt)

    def repair(self, root: Path, candidates: list[Candidate], verification: VerificationResult) -> GuardedApplyResult:
        if not candidates:
            return GuardedApplyResult(status="unsupported", summary=["no guarded candidates available for repair"], details={})
        if not self.is_available():
            return GuardedApplyResult(status="unsupported", summary=["codex cli is not available"], details={})
        prompt = self._build_repair_prompt(candidates, verification)
        return self._run(root, prompt)

    def _run(self, root: Path, prompt: str) -> GuardedApplyResult:
        with tempfile.TemporaryDirectory(prefix="refactorq-codex-") as temp_dir:
            temp_root = Path(temp_dir)
            schema_path = temp_root / "codex-output-schema.json"
            output_path = temp_root / "codex-output.json"
            schema_path.write_text(json.dumps(_OUTPUT_SCHEMA), encoding="utf-8")
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

    def _build_apply_prompt(self, candidate: Candidate) -> str:
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

    def _build_repair_prompt(self, candidates: list[Candidate], verification: VerificationResult) -> str:
        allowed_files = sorted({file for candidate in candidates for file in candidate.files})
        evidence: list[str] = []
        for check in verification.checks:
            if check.status == "failed":
                evidence.append(f"{check.name}: {' | '.join(check.evidence[:5])}")
        candidate_lines = [
            f"- {candidate.id}: {candidate.kind} in {candidate.files[0]} ({candidate.title})"
            for candidate in candidates
        ]
        return (
            "You are repairing a previously applied guarded refactor after verification failed.\n"
            "Modify only the allowed files and keep the original refactoring intent.\n"
            "Do not broaden scope, touch tests, configs, docs, or unrelated files.\n"
            "If safe repair is not possible, make no changes and return status no_change.\n\n"
            f"Allowed files: {', '.join(allowed_files)}\n"
            "Guarded candidates:\n"
            + "\n".join(candidate_lines)
            + "\n\n"
            + "Verification failures:\n"
            + ("\n".join(evidence) if evidence else "- verification failed without detailed evidence")
            + "\n\nReturn JSON matching the provided schema with touchedFiles, summary, and details.\n"
        )
