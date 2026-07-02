from __future__ import annotations

from refactorq.core.candidate import Candidate

from .models import PlanEdge


def _regions_overlap(left: Candidate, right: Candidate) -> bool:
    for left_region in left.anchor_regions:
        for right_region in right.anchor_regions:
            if left_region.file != right_region.file:
                continue
            if left_region.start_line <= right_region.end_line and right_region.start_line <= left_region.end_line:
                return True
    return False


def _shared_symbol_scope(left: Candidate, right: Candidate) -> bool:
    if left.language != right.language or left.scope != right.scope:
        return False
    return bool(set(left.symbols) & set(right.symbols))


def _same_file_non_local(left: Candidate, right: Candidate) -> bool:
    if left.scope == "local" and right.scope == "local":
        return False
    return bool(set(left.files) & set(right.files))


def batch_conflict_reason(candidate: Candidate, current: Candidate) -> str | None:
    if _regions_overlap(candidate, current):
        return f"candidate overlaps already selected batch candidate {current.id}"
    if current.id in candidate.conflicts or candidate.id in current.conflicts:
        return f"candidate explicitly conflicts with already selected batch candidate {current.id}"
    if _shared_symbol_scope(candidate, current):
        return f"candidate shares the same symbol and scope as already selected batch candidate {current.id}"
    if _same_file_non_local(candidate, current):
        return f"candidate touches the same file as non-local already selected batch candidate {current.id}"
    return None


def pairwise_conflict_reasons(left: Candidate, right: Candidate) -> list[str]:
    reasons: list[str] = []
    if _regions_overlap(left, right):
        reasons.append("overlapping anchor regions in the same file")
    elif _shared_symbol_scope(left, right):
        reasons.append("same symbol in the same language scope")
    elif _same_file_non_local(left, right):
        reasons.append("same file touched with at least one non-local candidate")
    if right.id in left.conflicts or left.id in right.conflicts:
        reasons.append("explicit conflict declared by candidate")
    return reasons


def _conflict_edge(left: Candidate, right: Candidate, reason: str) -> PlanEdge:
    first_id, second_id = sorted((left.id, right.id))
    return PlanEdge(fromId=first_id, toId=second_id, kind="conflict", reason=reason)


def _dependency_edge(from_candidate: Candidate, to_candidate: Candidate, reason: str) -> PlanEdge:
    return PlanEdge(fromId=from_candidate.id, toId=to_candidate.id, kind="dependency", reason=reason)


def _synergy_edge(left: Candidate, right: Candidate, reason: str) -> PlanEdge:
    first_id, second_id = sorted((left.id, right.id))
    return PlanEdge(fromId=first_id, toId=second_id, kind="synergy", reason=reason)


def _is_duplicate_extract_dependency(left: Candidate, right: Candidate) -> bool:
    return (
        left.kind == "duplicate_logic"
        and right.kind == "extract_function"
        and bool(set(left.files) & set(right.files))
        and bool(set(left.symbols) & set(right.symbols))
    )


def _is_cycle_split_dependency(left: Candidate, right: Candidate) -> bool:
    return (
        left.kind == "split_large_module"
        and right.kind == "reduce_cycle"
        and bool(set(left.files) & set(right.files))
    )


def _is_move_symbol_layer_dependency(left: Candidate, right: Candidate) -> bool:
    return (
        left.kind == "move_symbol"
        and right.kind == "layer_violation_fix"
        and bool(set(left.files) & set(right.files))
        and bool(set(left.symbols) & set(right.symbols))
    )


def _is_boundary_review_dependency(left: Candidate, right: Candidate) -> bool:
    return (
        left.boundary_impact.cross_language
        and right.kind == "custom"
        and right.id.startswith("boundary-review-")
        and bool(set(left.boundary_impact.contract_artifacts) & set(right.files))
    )


def _is_duplicate_extract_synergy(left: Candidate, right: Candidate) -> bool:
    kinds = {left.kind, right.kind}
    return kinds == {"duplicate_logic", "extract_function"} and bool(set(left.files) & set(right.files))


def _is_duplicate_remove_abstraction_synergy(left: Candidate, right: Candidate) -> bool:
    kinds = {left.kind, right.kind}
    return kinds == {"duplicate_logic", "remove_abstraction"} and bool(set(left.files) & set(right.files))


def _is_cycle_split_synergy(left: Candidate, right: Candidate) -> bool:
    kinds = {left.kind, right.kind}
    return kinds == {"reduce_cycle", "split_large_module"} and bool(set(left.files) & set(right.files))


def _is_layer_move_synergy(left: Candidate, right: Candidate) -> bool:
    kinds = {left.kind, right.kind}
    return (
        kinds == {"layer_violation_fix", "move_symbol"}
        and bool(set(left.files) & set(right.files))
        and bool(set(left.symbols) & set(right.symbols))
    )


def synergy_bonus(candidate: Candidate, selected: list[Candidate]) -> float:
    bonus = 0.0
    for current in selected:
        if _is_duplicate_extract_synergy(candidate, current):
            bonus += 0.18
        if _is_duplicate_remove_abstraction_synergy(candidate, current):
            bonus += 0.16
        if _is_cycle_split_synergy(candidate, current):
            bonus += 0.14
        if _is_layer_move_synergy(candidate, current):
            bonus += 0.15
    return bonus


def _add_edge(edges: list[PlanEdge], seen: set[tuple[str, str, str]], edge: PlanEdge) -> None:
    key = (edge.from_id, edge.to_id, edge.reason)
    if key not in seen:
        seen.add(key)
        edges.append(edge)


def _conflict_edges(candidates: list[Candidate]) -> list[PlanEdge]:
    edges: list[PlanEdge] = []
    seen: set[tuple[str, str, str]] = set()
    for index, left in enumerate(candidates):
        for right in candidates[index + 1 :]:
            for reason in pairwise_conflict_reasons(left, right):
                _add_edge(edges, seen, _conflict_edge(left, right, reason))
    return edges


def _synergy_edges(candidates: list[Candidate]) -> list[PlanEdge]:
    edges: list[PlanEdge] = []
    seen: set[tuple[str, str, str]] = set()
    for index, left in enumerate(candidates):
        for right in candidates[index + 1 :]:
            if _is_duplicate_extract_synergy(left, right):
                _add_edge(
                    edges,
                    seen,
                    _synergy_edge(left, right, "duplicate consolidation and extraction reinforce the same file refactor"),
                )
            if _is_duplicate_remove_abstraction_synergy(left, right):
                _add_edge(
                    edges,
                    seen,
                    _synergy_edge(left, right, "duplicate cleanup pairs with removing thin wrappers in the same file"),
                )
            if _is_cycle_split_synergy(left, right):
                _add_edge(
                    edges,
                    seen,
                    _synergy_edge(left, right, "cycle reduction and module splitting reinforce the same structural cleanup"),
                )
            if _is_layer_move_synergy(left, right):
                _add_edge(
                    edges,
                    seen,
                    _synergy_edge(left, right, "layer boundary review and symbol relocation target the same cross-layer import"),
                )
    return edges


def _dependency_edges(candidates: list[Candidate]) -> list[PlanEdge]:
    by_id = {candidate.id: candidate for candidate in candidates}
    edges: list[PlanEdge] = []
    seen: set[tuple[str, str, str]] = set()
    for candidate in candidates:
        for dependency_id in candidate.dependencies:
            if dependency_id in by_id:
                _add_edge(edges, seen, _dependency_edge(candidate, by_id[dependency_id], "explicit dependency declared by candidate"))
        for other in candidates:
            if candidate.id == other.id:
                continue
            if _is_duplicate_extract_dependency(candidate, other):
                _add_edge(edges, seen, _dependency_edge(candidate, other, "extract function before duplicate consolidation in the same file"))
            if _is_cycle_split_dependency(candidate, other):
                _add_edge(edges, seen, _dependency_edge(candidate, other, "reduce cycle before splitting the related module"))
            if _is_move_symbol_layer_dependency(candidate, other):
                _add_edge(edges, seen, _dependency_edge(candidate, other, "review layer violation before moving the shared boundary symbol"))
            if _is_boundary_review_dependency(candidate, other):
                _add_edge(edges, seen, _dependency_edge(candidate, other, "review boundary contract artifact before cross-language execution"))
    return edges


def plan_edges(candidates: list[Candidate]) -> list[PlanEdge]:
    edges = _conflict_edges(candidates)
    edges.extend(_dependency_edges(candidates))
    edges.extend(_synergy_edges(candidates))
    return sorted(edges, key=lambda edge: (edge.kind, edge.from_id, edge.to_id, edge.reason))
