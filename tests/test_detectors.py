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


def test_python_adapter_marks_multiline_unused_import_report_only(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text("from os import (\n    path,\n    getenv,\n)\n\nprint('hi')\n", encoding="utf-8")

    candidates = PythonAdapter().scan(tmp_path)

    unused_import = next(candidate for candidate in candidates if candidate.kind == "unused_import")
    assert unused_import.symbols == ["path"]
    assert unused_import.apply_mode_hint == "report_only"


def test_python_adapter_detects_unused_private_assignment_candidate(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text("_UNUSED = {\"ok\": [1, 2, 3]}\n\nprint('hi')\n", encoding="utf-8")

    candidates = PythonAdapter().scan(tmp_path)

    unused_symbol = next(candidate for candidate in candidates if candidate.kind == "unused_symbol")
    assert unused_symbol.symbols == ["_UNUSED"]
    assert unused_symbol.apply_mode_hint == "auto"


def test_python_adapter_skips_unused_private_assignment_with_call_initializer(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text("_UNUSED = dict(ok=True)\n\nprint('hi')\n", encoding="utf-8")

    candidates = PythonAdapter().scan(tmp_path)

    assert not any(candidate.kind == "unused_symbol" for candidate in candidates)


def test_python_adapter_detects_private_dead_code(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text("def _helper():\n    return 1\n\nprint('hi')\n", encoding="utf-8")

    candidates = PythonAdapter().scan(tmp_path)

    assert any(candidate.kind == "dead_code" and candidate.symbols == ["_helper"] for candidate in candidates)


def test_python_adapter_detects_private_class_dead_code(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text("class _Helper:\n    pass\n\nprint('hi')\n", encoding="utf-8")

    candidates = PythonAdapter().scan(tmp_path)

    assert any(candidate.kind == "dead_code" and candidate.symbols == ["_Helper"] for candidate in candidates)


def test_python_adapter_detects_remove_abstraction_candidate(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(
        "def normalize(value):\n    return value.strip().lower()\n\n"
        "def _normalize_wrapper(value):\n    return normalize(value)\n",
        encoding="utf-8",
    )

    candidates = PythonAdapter().scan(tmp_path)

    assert any(candidate.kind == "remove_abstraction" and candidate.symbols == ["_normalize_wrapper"] for candidate in candidates)


def test_python_adapter_detects_inline_function_candidate(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(
        "def _normalize_value(value):\n    cleaned = value.strip()\n    return cleaned.lower()\n\n"
        "def format_value(value):\n    return _normalize_value(value)\n",
        encoding="utf-8",
    )

    candidates = PythonAdapter().scan(tmp_path)

    assert any(candidate.kind == "inline_function" and candidate.symbols == ["_normalize_value"] for candidate in candidates)


def test_python_adapter_skips_inline_function_for_public_helper(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(
        "def normalize_value(value):\n    cleaned = value.strip()\n    return cleaned.lower()\n\n"
        "def format_value(value):\n    return normalize_value(value)\n",
        encoding="utf-8",
    )

    candidates = PythonAdapter().scan(tmp_path)

    assert not any(candidate.kind == "inline_function" and candidate.symbols == ["normalize_value"] for candidate in candidates)


def test_python_adapter_detects_extract_function_candidate(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    body = "\n".join([f"    value_{index} = input_value + {index}" for index in range(34)])
    sample.write_text(
        "def format_value(input_value):\n"
        f"{body}\n"
        "    return input_value\n",
        encoding="utf-8",
    )

    candidates = PythonAdapter().scan(tmp_path)

    extract_function = next(candidate for candidate in candidates if candidate.kind == "extract_function")
    assert extract_function.symbols == ["format_value"]
    assert extract_function.apply_mode_hint == "guarded"


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
    layer_violation = next(candidate for candidate in candidates if candidate.kind == "layer_violation_fix")
    assert layer_violation.boundary_impact.impact_level == "medium"
    assert layer_violation.boundary_impact.producer_side == ["backend/service.py"]
    assert layer_violation.boundary_impact.consumer_side == ["frontend/ui.py"]

    move_symbol = next(candidate for candidate in candidates if candidate.kind == "move_symbol")
    assert move_symbol.files == ["frontend/ui.py", "backend/service.py"]
    assert move_symbol.symbols == ["service"]
    assert move_symbol.boundary_impact.impact_level == "medium"
    assert move_symbol.boundary_impact.producer_side == ["backend/service.py"]
    assert move_symbol.boundary_impact.consumer_side == ["frontend/ui.py"]


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


def test_ts_worker_verify_preserves_command_for_generic_failures(tmp_path: Path) -> None:
    missing_root = tmp_path / "missing"
    worker = Path(__file__).resolve().parents[1] / "workers" / "ts-adapter" / "src" / "index.ts"
    request = json.dumps({"protocolVersion": PROTOCOL_VERSION, "capabilities": ["verify"], "command": "verify", "root": str(missing_root)})
    completed = subprocess.run(
        ["node", "--experimental-strip-types", str(worker)],
        input=request,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode != 0
    payload = json.loads(completed.stdout)

    assert payload["ok"] is False
    assert payload["command"] == "verify"
    assert payload["error"]["code"] == "execution_failed"


def test_ts_worker_marks_multiline_unused_import_report_only(tmp_path: Path) -> None:
    (tmp_path / "multi-import.ts").write_text(
        "import {\n"
        "  readFile,\n"
        "  writeFile,\n"
        "} from \"node:fs\";\n\n"
        "console.log(writeFile);\n",
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
    unused_import = next(candidate for candidate in payload["candidates"] if candidate["kind"] == "unused_import")

    assert unused_import["symbols"] == ["readFile"]
    assert unused_import["applyModeHint"] == "report_only"


def test_ts_worker_emits_unused_symbol_candidates_for_module_scope(tmp_path: Path) -> None:
    (tmp_path / "unused.ts").write_text(
        "export {};\n\n"
        "function helper() {\n  return 1;\n}\n\n"
        "class HelperClass {}\n\n"
        "const UNUSED_VALUE = { ok: true };\n\n"
        "console.log('ok');\n",
        encoding="utf-8",
    )
    (tmp_path / "consumer.ts").write_text(
        "console.log('consumer');\n",
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

    unused_symbols = {
        tuple(candidate["symbols"])
        for candidate in payload["candidates"]
        if candidate["kind"] == "unused_symbol"
    }
    assert ("helper",) in unused_symbols
    assert ("HelperClass",) in unused_symbols
    assert ("UNUSED_VALUE",) in unused_symbols


def test_ts_worker_skips_unused_symbol_candidates_for_script_globals_in_multi_file_repo(tmp_path: Path) -> None:
    (tmp_path / "globals.ts").write_text(
        "function helper() {\n  return 1;\n}\n\n"
        "class HelperClass {}\n\n"
        "const UNUSED_VALUE = { ok: true };\n",
        encoding="utf-8",
    )
    (tmp_path / "consumer.ts").write_text(
        "console.log(globalThis);\n",
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

    unused_symbols = {
        tuple(candidate["symbols"])
        for candidate in payload["candidates"]
        if candidate["kind"] == "unused_symbol"
    }
    assert ("helper",) not in unused_symbols
    assert ("HelperClass",) not in unused_symbols
    assert ("UNUSED_VALUE",) not in unused_symbols

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


def test_ts_worker_emits_inline_function_candidates(tmp_path: Path) -> None:
    (tmp_path / "inline.ts").write_text(
        "function _normalizeValue(value: string) {\n"
        "  const cleaned = value.trim();\n"
        "  return cleaned.toLowerCase();\n"
        "}\n\n"
        "function formatValue(value: string) {\n"
        "  return _normalizeValue(value);\n"
        "}\n",
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

    assert any(candidate["kind"] == "inline_function" and candidate["symbols"] == ["_normalizeValue"] for candidate in payload["candidates"])


def test_ts_worker_emits_extract_function_candidates_for_const_arrow(tmp_path: Path) -> None:
    body = "\n".join([f"  const value{index} = input + {index};" for index in range(38)])
    (tmp_path / "extract.ts").write_text(
        "const formatValue = (input: number) => {\n"
        f"{body}\n"
        "  return input;\n"
        "};\n",
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

    assert any(candidate["kind"] == "extract_function" and candidate["symbols"] == ["formatValue"] for candidate in payload["candidates"])


def test_ts_worker_emits_duplicate_logic_candidates_for_const_arrows(tmp_path: Path) -> None:
    (tmp_path / "dup-arrow.ts").write_text(
        "const first = (value: string) => {\n"
        "  const cleaned = value.trim();\n"
        "  return cleaned.toLowerCase();\n"
        "};\n\n"
        "const second = (value: string) => {\n"
        "  const cleaned = value.trim();\n"
        "  return cleaned.toLowerCase();\n"
        "};\n",
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

    assert any(candidate["kind"] == "duplicate_logic" and candidate["symbols"] == ["first", "second"] for candidate in payload["candidates"])


def test_ts_worker_emits_remove_abstraction_candidates_for_const_arrow_wrapper(tmp_path: Path) -> None:
    (tmp_path / "wrapper-arrow.ts").write_text(
        "const normalize = (value: string) => value.trim().toLowerCase();\n\n"
        "const _normalizeWrapper = (value: string) => normalize(value);\n",
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


def test_ts_worker_emits_inline_function_candidates_for_const_arrow(tmp_path: Path) -> None:
    (tmp_path / "inline-arrow.ts").write_text(
        "const _normalizeValue = (value: string) => {\n"
        "  const cleaned = value.trim();\n"
        "  return cleaned.toLowerCase();\n"
        "};\n\n"
        "const formatValue = (value: string) => _normalizeValue(value);\n",
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

    assert any(candidate["kind"] == "inline_function" and candidate["symbols"] == ["_normalizeValue"] for candidate in payload["candidates"])


def test_ts_worker_skips_inline_function_for_exported_const_arrow(tmp_path: Path) -> None:
    (tmp_path / "inline-exported.ts").write_text(
        "export const _normalizeValue = (value: string) => {\n"
        "  const cleaned = value.trim();\n"
        "  return cleaned.toLowerCase();\n"
        "};\n\n"
        "const formatValue = (value: string) => _normalizeValue(value);\n",
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

    assert not any(candidate["kind"] == "inline_function" and candidate["symbols"] == ["_normalizeValue"] for candidate in payload["candidates"])


def test_ts_worker_emits_remove_abstraction_candidates_for_const_function_expression(tmp_path: Path) -> None:
    (tmp_path / "wrapper-fnexpr.ts").write_text(
        "const normalize = function(value: string) {\n"
        "  return value.trim().toLowerCase();\n"
        "};\n\n"
        "const _normalizeWrapper = function(value: string) {\n"
        "  return normalize(value);\n"
        "};\n",
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


def test_ts_worker_emits_duplicate_logic_candidates_for_multi_declarator_const_statement(tmp_path: Path) -> None:
    (tmp_path / "dup-multi.ts").write_text(
        "const first = (value: string) => {\n"
        "  const cleaned = value.trim();\n"
        "  return cleaned.toLowerCase();\n"
        "}, second = (value: string) => {\n"
        "  const cleaned = value.trim();\n"
        "  return cleaned.toLowerCase();\n"
        "};\n",
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

    assert any(candidate["kind"] == "duplicate_logic" and candidate["symbols"] == ["first", "second"] for candidate in payload["candidates"])

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
    layer_violation = next(candidate for candidate in payload["candidates"] if candidate["kind"] == "layer_violation_fix")
    assert layer_violation["boundaryImpact"]["impactLevel"] == "medium"
    assert layer_violation["boundaryImpact"]["producerSide"] == ["backend/service.ts"]
    assert layer_violation["boundaryImpact"]["consumerSide"] == ["frontend/ui.ts"]

    move_symbol = next(candidate for candidate in payload["candidates"] if candidate["kind"] == "move_symbol")
    assert move_symbol["files"] == ["frontend/ui.ts", "backend/service.ts"]
    assert move_symbol["symbols"] == ["service"]
    assert move_symbol["boundaryImpact"]["impactLevel"] == "medium"
    assert move_symbol["boundaryImpact"]["producerSide"] == ["backend/service.ts"]
    assert move_symbol["boundaryImpact"]["consumerSide"] == ["frontend/ui.ts"]

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
