from __future__ import annotations

from typing import Any, Literal, Sequence, cast

from .models import ProofRecord, VerificationCheckResult, VerificationReadiness, VerificationResult
from .readiness import _BOUNDARY_ENFORCED_CHECKS, _proof_status, ordered_unique


def verification_result_from_report(
    *,
    status: Literal["passed", "failed"],
    checks: list[VerificationCheckResult],
    report: dict[str, Any],
) -> VerificationResult:
    finalized_report = _finalize_verification_report(report, checks)
    missing_predicates = cast(list[str], finalized_report["missingPredicates"])
    proof_refs = cast(list[str], finalized_report["proofRefs"])
    return VerificationResult(
        status=status,
        checks=checks,
        readiness=VerificationReadiness(
            ready=status == "passed" and not missing_predicates,
            proofStatus=cast(Any, finalized_report["proofStatus"]),
            missingPredicates=missing_predicates,
            proofRefs=proof_refs,
        ),
        proofRecords=_proof_records_from_report(finalized_report),
    )


def _finalize_verification_report(report: dict[str, Any], checks: Sequence[VerificationCheckResult]) -> dict[str, Any]:
    failed_boundary_predicates: list[str] = []
    failed_check_kinds = {check.kind for check in checks if check.status == "failed"}
    if any(check.name == "boundary_contracts" and check.status == "failed" for check in checks):
        failed_boundary_predicates.append("boundary_contracts")
    if any(check.name == "boundary_integration" and check.status == "failed" for check in checks):
        failed_boundary_predicates.append("boundary_integration")

    proof_refs = cast(list[str], report["proofRefs"])
    finalized_proof_refs = [
        proof_ref
        for proof_ref in proof_refs
        if not proof_ref.startswith("check:") or proof_ref.split(":", 1)[1] not in failed_check_kinds
    ]
    missing_predicates = ordered_unique(
        [
            *cast(list[str], report["missingPredicates"]),
            *failed_boundary_predicates,
            *[f"check:{kind}" for kind in sorted(failed_check_kinds & _BOUNDARY_ENFORCED_CHECKS)],
        ]
    )
    return {
        **report,
        "proofStatus": _proof_status(
            boundary_sensitive=bool(cast(list[dict[str, Any]], report["boundaryCandidates"])),
            missing_predicates=missing_predicates,
            proof_refs=finalized_proof_refs,
        ),
        "missingPredicates": missing_predicates,
        "proofRefs": finalized_proof_refs,
    }


def _proof_records_from_report(report: dict[str, Any]) -> list[ProofRecord]:
    proof_status = cast(str, report["proofStatus"])
    if proof_status == "not_applicable":
        return []
    proof_refs = cast(list[str], report["proofRefs"])
    return [
        ProofRecord(
            proofId=f"verification-proof-{index + 1}",
            kind=cast(Any, _proof_kind_from_ref(proof_ref)),
            status=cast(Any, proof_status),
            predicate=proof_ref,
            references=[proof_ref],
        )
        for index, proof_ref in enumerate(proof_refs)
    ]


def _proof_kind_from_ref(proof_ref: str) -> str:
    if proof_ref.startswith("artifact:"):
        return "boundary_contract"
    if proof_ref == "check:integration_test":
        return "boundary_integration"
    if proof_ref.startswith("check:"):
        check_name = proof_ref.split(":", 1)[1]
        if check_name in {"parse", "typecheck", "lint", "build", "unit_test", "integration_test"}:
            return check_name
    return "manual_review"
