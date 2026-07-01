from __future__ import annotations

from pathlib import Path

from .base import LanguageAdapter
from .python import PythonAdapter
from .typescript import TypeScriptAdapter


def available_adapters() -> list[LanguageAdapter]:
    return [PythonAdapter(), TypeScriptAdapter()]


def detect_adapters(root: Path) -> list[LanguageAdapter]:
    return [adapter for adapter in available_adapters() if adapter.supports(root)]
