from __future__ import annotations

from pathlib import Path

from refactorq.core.filesystem import walk_repo_files


def snapshot_repo(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in walk_repo_files(root)}


def changed_paths(before: dict[str, bytes], after: dict[str, bytes]) -> list[str]:
    return [rel_path for rel_path in sorted(set(before) | set(after)) if before.get(rel_path) != after.get(rel_path)]


def restore_snapshot(root: Path, snapshot: dict[str, bytes]) -> bool:
    current = snapshot_repo(root)
    restored = False
    for rel_path in sorted(set(snapshot) | set(current)):
        target = root / rel_path
        original = snapshot.get(rel_path)
        current_bytes = current.get(rel_path)
        if original == current_bytes:
            continue
        if original is None:
            if target.exists():
                target.unlink()
                restored = True
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(original)
        restored = True
    return restored
