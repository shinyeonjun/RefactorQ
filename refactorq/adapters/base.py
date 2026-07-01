from __future__ import annotations

from pathlib import Path
from typing import Protocol

from refactorq.core.candidate import Candidate


class LanguageAdapter(Protocol):
    name: str
    extensions: tuple[str, ...]

    def supports(self, root: Path) -> bool: ...

    def scan(self, root: Path) -> list[Candidate]: ...
