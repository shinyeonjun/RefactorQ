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


def test_typescript_adapter_preserves_worker_candidate_order(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    sample = tmp_path / "sample.ts"
    sample.write_text('import { readFile } from "node:fs";\n\nconsole.log("hi");\n', encoding="utf-8")

    unsorted_payload = {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": PROTOCOL_CAPABILITIES,
        "ok": True,
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
                "confidence": 0.9,
                "applyModeHint": "auto",
                "requiredChecks": ["parse", "lint", "typecheck"],
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
                "confidence": 0.9,
                "applyModeHint": "auto",
                "requiredChecks": ["parse", "lint", "typecheck"],
                "provenance": {"detectors": ["ts-worker-unused-import"], "evidence": ["line:1"]},
            },
        ],
    }
    monkeypatch.setattr(TypeScriptAdapter, "_invoke_worker", lambda self, root: unsorted_payload)

    candidates = TypeScriptAdapter().scan(tmp_path)

    assert [candidate.files[0] for candidate in candidates] == ["zeta.ts", "alpha.ts"]


def test_typescript_adapter_reports_worker_contract_failures(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    sample = tmp_path / "sample.ts"
    sample.write_text('import { readFile } from "node:fs";\n\nconsole.log("hi");\n', encoding="utf-8")

    monkeypatch.setattr(TypeScriptAdapter, "_invoke_worker", lambda self, root: {"protocolVersion": PROTOCOL_VERSION + 1, "capabilities": PROTOCOL_CAPABILITIES, "ok": True, "candidates": []})

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

    monkeypatch.setattr(TypeScriptAdapter, "_invoke_worker", raise_non_zero)

    candidates = TypeScriptAdapter().scan(tmp_path)

    assert len(candidates) == 1
    failure = candidates[0]
    assert failure.kind == "custom"
    assert any(evidence.startswith("code:non_zero_exit") for evidence in failure.provenance.evidence)
    assert not any(candidate.kind == "unused_import" for candidate in candidates)


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
    assert payload["capabilities"] == PROTOCOL_CAPABILITIES
    assert [candidate["files"][0] for candidate in payload["candidates"]] == ["alpha.ts", "zeta.ts"]
    first_candidate = payload["candidates"][0]
    for key in ["contextSignals", "boundaryImpact", "dependencies", "conflicts"]:
        assert key in first_candidate
