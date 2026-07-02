from __future__ import annotations

from refactorq.adapters.python.common import benefit, diff, layer_boundary_impact, region, risk
from refactorq.core.candidate.models import Candidate, Provenance


def layer_boundary_candidates(
    rel_path: str,
    layer_violations: list[tuple[int, str, tuple[str, ...]]],
) -> list[Candidate]:
    candidates: list[Candidate] = []
    for line_number, target_rel_path, imported_symbols in sorted(layer_violations):
        candidates.append(_layer_violation_candidate(rel_path, line_number, target_rel_path, imported_symbols))
        if imported_symbols:
            candidates.append(_move_symbol_candidate(rel_path, line_number, target_rel_path, imported_symbols))
    return candidates


def _layer_violation_candidate(
    rel_path: str,
    line_number: int,
    target_rel_path: str,
    imported_symbols: tuple[str, ...],
) -> Candidate:
    return Candidate(
        id=f"py-layer-violation-{rel_path}-{line_number}-{target_rel_path.replace('/', '-')}",
        kind="layer_violation_fix",
        title=f"Review layer-violating import in {rel_path}",
        description=(
            f"Import at line {line_number} crosses between `{rel_path}` and `{target_rel_path}`,"
            " suggesting a layer boundary violation"
        ),
        language="python",
        scope="architecture",
        source=["graph"],
        files=[rel_path, target_rel_path],
        symbols=list(imported_symbols),
        anchorRegions=[region(rel_path, line_number, line_number)],
        estimatedBenefit=benefit({"maintainabilityGain": 0.32}),
        estimatedRisk=risk({"semanticRisk": 0.22, "apiRisk": 0.1, "testRisk": 0.18, "conflictRisk": 0.12}),
        estimatedDiff=diff({"filesTouched": 2, "linesAdded": 4, "linesModified": 8}),
        boundaryImpact=layer_boundary_impact(target_rel_path, rel_path),
        confidence=0.69,
        applyModeHint="report_only",
        requiredChecks=["parse", "lint", "typecheck", "unit_test"],
        provenance=Provenance(
            detectors=["python-import-layer-violation"],
            evidence=[f"line:{line_number}", f"target:{target_rel_path}"],
        ),
    )


def _move_symbol_candidate(
    rel_path: str,
    line_number: int,
    target_rel_path: str,
    imported_symbols: tuple[str, ...],
) -> Candidate:
    symbol_summary = ", ".join(f"`{symbol}`" for symbol in imported_symbols)
    return Candidate(
        id=f"py-move-symbol-{rel_path}-{line_number}-{target_rel_path.replace('/', '-')}",
        kind="move_symbol",
        title=f"Review moving imported boundary symbols from {target_rel_path}",
        description=(
            f"Imported symbols {symbol_summary} cross between `{rel_path}` and `{target_rel_path}` and may"
            " need relocation behind a clearer module boundary"
        ),
        language="python",
        scope="architecture",
        source=["graph"],
        files=[rel_path, target_rel_path],
        symbols=list(imported_symbols),
        anchorRegions=[region(rel_path, line_number, line_number)],
        estimatedBenefit=benefit({"maintainabilityGain": 0.28}),
        estimatedRisk=risk({"semanticRisk": 0.28, "apiRisk": 0.16, "testRisk": 0.22, "conflictRisk": 0.14}),
        estimatedDiff=diff({"filesTouched": 2, "linesAdded": 6, "linesModified": 10}),
        boundaryImpact=layer_boundary_impact(target_rel_path, rel_path),
        confidence=0.64,
        applyModeHint="report_only",
        requiredChecks=["parse", "lint", "typecheck", "unit_test"],
        provenance=Provenance(
            detectors=["python-import-move-symbol"],
            evidence=[
                f"line:{line_number}",
                f"target:{target_rel_path}",
                *[f"symbol:{symbol}" for symbol in imported_symbols],
            ],
        ),
    )
