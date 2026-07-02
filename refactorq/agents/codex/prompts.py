from __future__ import annotations

from .models import GuardedApplyRequest, GuardedRepairRequest


def build_apply_prompt(request: GuardedApplyRequest) -> str:
    candidate = request.candidate
    required_checks = ", ".join(request.scope.required_checks) if request.scope.required_checks else "none"
    region_lines = [
        f"- {region.file}: lines {region.start_line}-{region.end_line}" for region in request.scope.anchor_regions
    ]
    symbol_summary = ", ".join(candidate.symbols) if candidate.symbols else "none"
    return (
        "You are applying one guarded refactoring candidate inside an existing repository.\n"
        "Modify only the allowed file. Preserve behavior and public interfaces.\n"
        "Do not touch tests, docs, configs, or any other files.\n"
        "Do not invent new candidate IDs or broaden the selected scope.\n"
        "If you cannot complete the candidate safely, make no changes and return status no_change.\n\n"
        f"Candidate ID: {candidate.id}\n"
        f"Selected candidate IDs: {', '.join(request.scope.candidate_ids)}\n"
        f"Allowed files: {', '.join(request.scope.allowed_files)}\n"
        f"Kind: {candidate.kind}\n"
        f"Language: {candidate.language}\n"
        f"File: {candidate.files[0]}\n"
        f"Symbols: {symbol_summary}\n"
        "Candidate regions:\n"
        + "\n".join(region_lines)
        + "\n"
        + f"Title: {candidate.title}\n"
        + f"Description: {candidate.description}\n"
        + f"Required checks: {required_checks}\n\n"
        + f"Preferred implementation: {_preferred_apply_implementation(candidate.kind)}\n\n"
        + "Return JSON matching the provided schema with touchedFiles and summary.\n"
    )


def build_repair_prompt(request: GuardedRepairRequest) -> str:
    evidence: list[str] = []
    for check in request.verification.checks:
        if check.status == "failed":
            evidence.append(f"{check.name}: {' | '.join(check.evidence[:5])}")
    candidate_lines = [
        f"- {candidate.id}: {candidate.kind} in {candidate.files[0]} ({candidate.title})"
        for candidate in request.candidates
    ]
    return (
        "You are repairing a previously applied guarded refactor after verification failed.\n"
        "Modify only the allowed files and keep the original refactoring intent.\n"
        "Do not broaden scope, touch tests, configs, docs, or unrelated files.\n"
        "Do not invent new candidate IDs or broaden the selected scope.\n"
        "If safe repair is not possible, make no changes and return status no_change.\n\n"
        f"Selected candidate IDs: {', '.join(request.scope.candidate_ids)}\n"
        f"Allowed files: {', '.join(request.scope.allowed_files)}\n"
        "Guarded candidates:\n"
        + "\n".join(candidate_lines)
        + "\n\n"
        + "Verification failures:\n"
        + ("\n".join(evidence) if evidence else "- verification failed without detailed evidence")
        + "\n\nReturn JSON matching the provided schema with touchedFiles and summary.\n"
    )


def _preferred_apply_implementation(kind: str) -> str:
    if kind == "duplicate_logic":
        return (
            "consolidate the duplicate logic into a shared local helper or a single canonical implementation"
            " while preserving every public or top-level call site behavior."
        )
    if kind == "inline_function":
        return (
            "inline the private same-file helper into its single caller, then delete the helper while preserving"
            " behavior and public interfaces."
        )
    if kind == "remove_abstraction":
        return (
            "remove or inline the thin wrapper abstraction, keeping behavior and public interfaces stable"
            " while simplifying same-file call paths."
        )
    return (
        "extract a small private helper or equivalent local refactor so the target function becomes shorter"
        " and clearer without changing behavior."
    )
