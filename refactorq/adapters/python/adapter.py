from __future__ import annotations

from pathlib import Path
from collections import defaultdict

from refactorq.adapters.python.file_scan import scan_file
from refactorq.adapters.python.graph import build_cycle_candidates, known_python_modules, module_name
from refactorq.core.candidate.models import Candidate
from refactorq.core.filesystem import walk_source_files


class PythonAdapter:
    name: str = "python"
    extensions: tuple[str, ...] = (".py",)

    def supports(self, root: Path) -> bool:
        return any(True for _ in walk_source_files(root, self.extensions))

    def scan(self, root: Path) -> list[Candidate]:
        candidates: list[Candidate] = []
        module_to_file = known_python_modules(root)
        graph: dict[str, set[str]] = defaultdict(set)
        for path in walk_source_files(root, self.extensions):
            file_candidates, imports = scan_file(root, path, module_to_file)
            candidates.extend(file_candidates)
            current_module = module_name(root, path)
            if current_module:
                graph.setdefault(current_module, set()).update(imports)
        for name in module_to_file:
            graph.setdefault(name, set())
        candidates.extend(build_cycle_candidates(graph, module_to_file))
        return candidates
