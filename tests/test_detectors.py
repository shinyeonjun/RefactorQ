import json
import subprocess
from pathlib import Path

from pytest import MonkeyPatch

from refactorq.adapters.python import PythonAdapter
from refactorq.adapters.typescript import TypeScriptAdapter
from refactorq.core.worker_protocol import PROTOCOL_CAPABILITIES, PROTOCOL_VERSION



def test_python_adapter_detects_unused_import(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text("import os\n\nprint('hi')\n", encoding="utf-8")

    candidates = PythonAdapter().scan(tmp_path)

    assert any(candidate.kind == "unused_import" for candidate in candidates)


def test_python_adapter_detects_private_dead_code(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text("def _helper():\n    return 1\n\nprint('hi')\n", encoding="utf-8")

    candidates = PythonAdapter().scan(tmp_path)

    assert any(candidate.kind == "dead_code" and candidate.symbols == ["_helper"] for candidate in candidates)


def test_python_adapter_detects_remove_abstraction_candidate(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(
        "def normalize(value):\n    return value.strip().lower()\n\n"
        "def _normalize_wrapper(value):\n    return normalize(value)\n",
        encoding="utf-8",
    )

    candidates = PythonAdapter().scan(tmp_path)

    assert any(candidate.kind == "remove_abstraction" and candidate.symbols == ["_normalize_wrapper"] for candidate in candidates)


def test_python_adapter_detects_large_module_candidate(tmp_path: Path) -> None:
    sample = tmp_path / "large_module.py"
    sample.write_text("\n".join([f"value_{index} = {index}" for index in range(20)]), encoding="utf-8")

    candidates = PythonAdapter().scan(tmp_path)

    assert any(candidate.kind == "split_large_module" and candidate.files == ["large_module.py"] for candidate in candidates)


def test_python_adapter_skips_init_reexport_imports(tmp_path: Path) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("from .service import Api\n", encoding="utf-8")
    (package / "service.py").write_text("class Api:\n    pass\n", encoding="utf-8")

    candidates = PythonAdapter().scan(tmp_path)

    assert not any(candidate.kind == "unused_import" and candidate.files == ["pkg/__init__.py"] for candidate in candidates)


def test_python_adapter_detects_layer_violation_candidate(tmp_path: Path) -> None:
    backend = tmp_path / "backend"
    frontend = tmp_path / "frontend"
    backend.mkdir()
    frontend.mkdir()
    (backend / "service.py").write_text("value = 1\n", encoding="utf-8")
    (frontend / "ui.py").write_text("from backend import service\n", encoding="utf-8")

    candidates = PythonAdapter().scan(tmp_path)

    assert any(candidate.kind == "layer_violation_fix" and candidate.files == ["frontend/ui.py", "backend/service.py"] for candidate in candidates)


def test_python_adapter_detects_import_cycle_candidate(tmp_path: Path) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "a.py").write_text("from . import b\n", encoding="utf-8")
    (package / "b.py").write_text("from . import a\n", encoding="utf-8")

    candidates = PythonAdapter().scan(tmp_path)

    assert any(candidate.kind == "reduce_cycle" and candidate.files == ["pkg/a.py", "pkg/b.py"] for candidate in candidates)



def test_typescript_adapter_preserves_worker_candidate_order(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    sample = tmp_path / "sample.ts"
    sample.write_text('import { readFile } from "node:fs";\n\nconsole.log("hi");\n', encoding="utf-8")

    unsorted_payload = {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": PROTOCOL_CAPABILITIES,
        "ok": True,
        "command": "scan",
        "candidates": [
            {
                "id": "ts-unused-import-zeta-1-zeta",
                "kind": "unused_import",
                "title": "Remove unused import zeta",
                "description": "Unused TypeScript import `zeta` in zeta.ts",
                "language": "typescript",
                "scope": "local",
                "source": ["static"],
                "files": ["zeta.ts"],
                "symbols": ["zeta"],
                "anchorRegions": [{"file": "zeta.ts", "startLine": 1, "endLine": 1}],
                "estimatedBenefit": {"maintainabilityGain": 0.08},
                "estimatedRisk": {"semanticRisk": 0.02, "conflictRisk": 0.03},
                "estimatedDiff": {"filesTouched": 1, "linesDeleted": 1, "linesModified": 1},
                "contextSignals": {},
                "boundaryImpact": {},
                "confidence": 0.9,
                "applyModeHint": "auto",
                "requiredChecks": ["parse", "lint", "typecheck"],
                "dependencies": [],
                "conflicts": [],
                "provenance": {"detectors": ["ts-worker-unused-import"], "evidence": ["line:1"]},
            },
            {
                "id": "ts-unused-import-alpha-1-alpha",
                "kind": "unused_import",
                "title": "Remove unused import alpha",
                "description": "Unused TypeScript import `alpha` in alpha.ts",
                "language": "typescript",
                "scope": "local",
                "source": ["static"],
                "files": ["alpha.ts"],
                "symbols": ["alpha"],
                "anchorRegions": [{"file": "alpha.ts", "startLine": 1, "endLine": 1}],
                "estimatedBenefit": {"maintainabilityGain": 0.08},
                "estimatedRisk": {"semanticRisk": 0.02, "conflictRisk": 0.03},
                "estimatedDiff": {"filesTouched": 1, "linesDeleted": 1, "linesModified": 1},
                "contextSignals": {},
                "boundaryImpact": {},
                "confidence": 0.9,
                "applyModeHint": "auto",
                "requiredChecks": ["parse", "lint", "typecheck"],
                "dependencies": [],
                "conflicts": [],
                "provenance": {"detectors": ["ts-worker-unused-import"], "evidence": ["line:1"]},
            },
        ],
    }
    monkeypatch.setattr(TypeScriptAdapter, "_invoke_scan", lambda self, root: unsorted_payload)

    candidates = TypeScriptAdapter().scan(tmp_path)

    assert [candidate.files[0] for candidate in candidates] == ["zeta.ts", "alpha.ts"]


def test_typescript_adapter_reports_worker_contract_failures(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    sample = tmp_path / "sample.ts"
    sample.write_text('import { readFile } from "node:fs";\n\nconsole.log("hi");\n', encoding="utf-8")

    monkeypatch.setattr(
        TypeScriptAdapter,
        "_invoke_scan",
        lambda self, root: {"protocolVersion": PROTOCOL_VERSION + 1, "capabilities": PROTOCOL_CAPABILITIES, "ok": True, "command": "scan", "candidates": []},
    )

    candidates = TypeScriptAdapter().scan(tmp_path)

    assert len(candidates) == 1
    failure = candidates[0]
    assert failure.kind == "custom"
    assert failure.confidence == 0.0
    assert failure.apply_mode_hint == "report_only"
    assert failure.provenance.detectors == ["ts-worker-bridge"]
    assert any(evidence.startswith("code:version_mismatch") for evidence in failure.provenance.evidence)


def test_typescript_adapter_reports_worker_process_failures(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    sample = tmp_path / "sample.ts"
    sample.write_text('import { readFile } from "node:fs";\n\nconsole.log("hi");\n', encoding="utf-8")

    def raise_non_zero(self: TypeScriptAdapter, root: Path) -> object:
        raise subprocess.CalledProcessError(1, ["node", "worker"], output="not-json", stderr="boom")

    monkeypatch.setattr(TypeScriptAdapter, "_invoke_scan", raise_non_zero)

    candidates = TypeScriptAdapter().scan(tmp_path)

    assert len(candidates) == 1
    failure = candidates[0]
    assert failure.kind == "custom"
    assert any(evidence.startswith("code:non_zero_exit") for evidence in failure.provenance.evidence)
    assert not any(candidate.kind == "unused_import" for candidate in candidates)


def test_typescript_adapter_verify_surfaces_failed_checks(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    sample = tmp_path / "sample.ts"
    sample.write_text("const broken: string = 123;\n", encoding="utf-8")

    monkeypatch.setattr(
        TypeScriptAdapter,
        "_invoke_verify",
        lambda self, root: {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": PROTOCOL_CAPABILITIES,
            "ok": True,
            "command": "verify",
            "checks": [
                {
                    "name": "typescript_typecheck",
                    "kind": "typecheck",
                    "status": "failed",
                    "evidence": ["sample.ts:1:1 mock failure"],
                    "details": {"diagnosticCount": 1},
                }
            ],
        },
    )

    checks = TypeScriptAdapter().verify(tmp_path)

    assert len(checks) == 1
    assert checks[0].status == "failed"
    assert checks[0].name == "typescript_typecheck"


def test_ts_worker_emits_protocol_version_and_sorted_candidates(tmp_path: Path) -> None:
    (tmp_path / "zeta.ts").write_text('import { readFile } from "node:fs";\n\nconsole.log("zeta");\n', encoding="utf-8")
    (tmp_path / "alpha.ts").write_text('import { writeFile } from "node:fs";\n\nconsole.log("alpha");\n', encoding="utf-8")

    worker = Path(__file__).resolve().parents[1] / "workers" / "ts-adapter" / "src" / "index.ts"
    request = json.dumps({"protocolVersion": PROTOCOL_VERSION, "capabilities": ["scan"], "command": "scan", "root": str(tmp_path)})
    completed = subprocess.run(
        ["node", "--experimental-strip-types", str(worker)],
        input=request,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )

    payload = json.loads(completed.stdout)

    assert payload["protocolVersion"] == PROTOCOL_VERSION
    assert payload["ok"] is True
    assert payload["command"] == "scan"
    assert payload["capabilities"] == PROTOCOL_CAPABILITIES
    assert [candidate["files"][0] for candidate in payload["candidates"]] == ["alpha.ts", "zeta.ts"]
    first_candidate = payload["candidates"][0]
    for key in ["contextSignals", "boundaryImpact", "dependencies", "conflicts"]:
        assert key in first_candidate


def test_ts_worker_verify_reports_semantic_failures(tmp_path: Path) -> None:
    (tmp_path / "broken.ts").write_text("const broken: string = 123;\n", encoding="utf-8")

    worker = Path(__file__).resolve().parents[1] / "workers" / "ts-adapter" / "src" / "index.ts"
    request = json.dumps({"protocolVersion": PROTOCOL_VERSION, "capabilities": ["verify"], "command": "verify", "root": str(tmp_path)})
    completed = subprocess.run(
        ["node", "--experimental-strip-types", str(worker)],
        input=request,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )

    payload = json.loads(completed.stdout)

    assert payload["protocolVersion"] == PROTOCOL_VERSION
    assert payload["ok"] is True
    assert payload["command"] == "verify"
    statuses = {check["name"]: check["status"] for check in payload["checks"]}
    assert statuses["typescript_parse"] == "passed"
    assert statuses["typescript_typecheck"] == "failed"


def test_ts_worker_emits_unused_symbol_candidates(tmp_path: Path) -> None:
    (tmp_path / "unused.ts").write_text("function helper() {\n  return 1;\n}\n\nconsole.log('ok');\n", encoding="utf-8")

    worker = Path(__file__).resolve().parents[1] / "workers" / "ts-adapter" / "src" / "index.ts"
    request = json.dumps({"protocolVersion": PROTOCOL_VERSION, "capabilities": ["scan"], "command": "scan", "root": str(tmp_path)})
    completed = subprocess.run(
        ["node", "--experimental-strip-types", str(worker)],
        input=request,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )

    payload = json.loads(completed.stdout)

    assert any(candidate["kind"] == "unused_symbol" and candidate["symbols"] == ["helper"] for candidate in payload["candidates"])

def test_ts_worker_emits_remove_abstraction_candidates(tmp_path: Path) -> None:
    (tmp_path / "wrapper.ts").write_text(
        "function normalize(value: string) {\n  return value.trim().toLowerCase();\n}\n\n"
        "function _normalizeWrapper(value: string) {\n  return normalize(value);\n}\n",
        encoding="utf-8",
    )

    worker = Path(__file__).resolve().parents[1] / "workers" / "ts-adapter" / "src" / "index.ts"
    request = json.dumps({"protocolVersion": PROTOCOL_VERSION, "capabilities": ["scan"], "command": "scan", "root": str(tmp_path)})
    completed = subprocess.run(
        ["node", "--experimental-strip-types", str(worker)],
        input=request,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )

    payload = json.loads(completed.stdout)

    assert any(candidate["kind"] == "remove_abstraction" and candidate["symbols"] == ["_normalizeWrapper"] for candidate in payload["candidates"])

def test_ts_worker_emits_large_module_candidates(tmp_path: Path) -> None:
    statements = [f"const value{index} = {index};" for index in range(20)]
    (tmp_path / "large.ts").write_text("\n".join(statements) + "\n", encoding="utf-8")

    worker = Path(__file__).resolve().parents[1] / "workers" / "ts-adapter" / "src" / "index.ts"
    request = json.dumps({"protocolVersion": PROTOCOL_VERSION, "capabilities": ["scan"], "command": "scan", "root": str(tmp_path)})
    completed = subprocess.run(
        ["node", "--experimental-strip-types", str(worker)],
        input=request,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )

    payload = json.loads(completed.stdout)

    assert any(candidate["kind"] == "split_large_module" and candidate["files"] == ["large.ts"] for candidate in payload["candidates"])

def test_ts_worker_emits_layer_violation_candidates(tmp_path: Path) -> None:
    backend = tmp_path / "backend"
    frontend = tmp_path / "frontend"
    backend.mkdir()
    frontend.mkdir()
    (backend / "service.ts").write_text("export const service = 1;\n", encoding="utf-8")
    (frontend / "ui.ts").write_text('import { service } from "../backend/service";\nconsole.log(service);\n', encoding="utf-8")

    worker = Path(__file__).resolve().parents[1] / "workers" / "ts-adapter" / "src" / "index.ts"
    request = json.dumps({"protocolVersion": PROTOCOL_VERSION, "capabilities": ["scan"], "command": "scan", "root": str(tmp_path)})
    completed = subprocess.run(
        ["node", "--experimental-strip-types", str(worker)],
        input=request,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )

    payload = json.loads(completed.stdout)

    assert any(candidate["kind"] == "layer_violation_fix" and candidate["files"] == ["frontend/ui.ts", "backend/service.ts"] for candidate in payload["candidates"])

def test_ts_worker_emits_reduce_cycle_candidates(tmp_path: Path) -> None:
    (tmp_path / "a.ts").write_text('import "./b";\nexport const a = 1;\n', encoding="utf-8")
    (tmp_path / "b.ts").write_text('import "./a";\nexport const b = 1;\n', encoding="utf-8")

    worker = Path(__file__).resolve().parents[1] / "workers" / "ts-adapter" / "src" / "index.ts"
    request = json.dumps({"protocolVersion": PROTOCOL_VERSION, "capabilities": ["scan"], "command": "scan", "root": str(tmp_path)})
    completed = subprocess.run(
        ["node", "--experimental-strip-types", str(worker)],
        input=request,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )

    payload = json.loads(completed.stdout)

    assert any(candidate["kind"] == "reduce_cycle" and candidate["files"] == ["a.ts", "b.ts"] for candidate in payload["candidates"])

def test_python_adapter_detects_duplicate_logic_candidate(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(
        "def first(value):\n    normalized = value.strip()\n    return normalized.lower()\n\n"
        "def second(value):\n    normalized = value.strip()\n    return normalized.lower()\n",
        encoding="utf-8",
    )

    candidates = PythonAdapter().scan(tmp_path)

    duplicate = next(candidate for candidate in candidates if candidate.kind == "duplicate_logic")
    assert duplicate.files == ["sample.py"]
    assert duplicate.symbols == ["first", "second"]
    assert duplicate.apply_mode_hint == "guarded"


def test_ts_worker_emits_duplicate_logic_candidates(tmp_path: Path) -> None:
    (tmp_path / "dup.ts").write_text(
        "function first(value: string) {\n  const normalized = value.trim();\n  return normalized.toLowerCase();\n}\n\n"
        "function second(value: string) {\n  const normalized = value.trim();\n  return normalized.toLowerCase();\n}\n",
        encoding="utf-8",
    )

    worker = Path(__file__).resolve().parents[1] / "workers" / "ts-adapter" / "src" / "index.ts"
    request = json.dumps({"protocolVersion": PROTOCOL_VERSION, "capabilities": ["scan"], "command": "scan", "root": str(tmp_path)})
    completed = subprocess.run(
        ["node", "--experimental-strip-types", str(worker)],
        input=request,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )

    payload = json.loads(completed.stdout)

    duplicate = next(candidate for candidate in payload["candidates"] if candidate["kind"] == "duplicate_logic")
    assert duplicate["files"] == ["dup.ts"]
    assert duplicate["symbols"] == ["first", "second"]
    assert duplicate["applyModeHint"] == "guarded"
