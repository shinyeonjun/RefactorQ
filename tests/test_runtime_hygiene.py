from __future__ import annotations

from pathlib import Path

from refactorq.core.filesystem import walk_repo_files, walk_source_files
from refactorq.core.service import RefactorQService


def test_walk_repo_files_ignores_gjc_runtime_paths(tmp_path: Path) -> None:
    (tmp_path / "sample.py").write_text("print('ok')\n", encoding="utf-8")
    runtime_file = tmp_path / ".gjc" / "_session-1" / "state" / "team" / "worker-1" / "shadow.py"
    runtime_file.parent.mkdir(parents=True)
    runtime_file.write_text("print('shadow')\n", encoding="utf-8")

    repo_files = [path.relative_to(tmp_path).as_posix() for path in walk_repo_files(tmp_path)]
    source_files = [path.relative_to(tmp_path).as_posix() for path in walk_source_files(tmp_path, (".py",))]

    assert repo_files == ["sample.py"]
    assert source_files == ["sample.py"]


def test_service_report_ignores_gjc_runtime_artifacts(tmp_path: Path) -> None:
    (tmp_path / "sample.py").write_text("import os\n", encoding="utf-8")
    (tmp_path / "sample.ts").write_text("const unusedValue = 1;\n", encoding="utf-8")

    runtime_root = tmp_path / ".gjc" / "_session-1" / "state" / "team" / "worker-1"
    runtime_root.mkdir(parents=True)
    (runtime_root / "sample.py").write_text("import os\n", encoding="utf-8")
    (runtime_root / "sample.ts").write_text("const unusedValue = 1;\n", encoding="utf-8")

    result = RefactorQService().report(tmp_path, "report")

    candidate_files = {
        path
        for candidate in result.plan.selected_candidates
        for path in candidate.files
    }
    candidate_files.update(
        path
        for excluded in result.plan.excluded_candidates
        for path in excluded.candidate.files
    )

    assert result.repo.python_files == 1
    assert result.repo.typescript_files == 1
    assert result.plan.candidate_count == 2
    assert candidate_files == {"sample.py", "sample.ts"}


def test_service_verify_ignores_gjc_runtime_typescript_copies(tmp_path: Path) -> None:
    (tmp_path / "sample.ts").write_text("const ok = 1;\n", encoding="utf-8")

    runtime_root = tmp_path / ".gjc" / "_session-1" / "state" / "team" / "worker-1"
    runtime_root.mkdir(parents=True)
    (runtime_root / "broken.ts").write_text(
        'import missing from "missing";\nconsole.log(missing);\n',
        encoding="utf-8",
    )

    result = RefactorQService().verify(tmp_path)

    assert result.status == "passed"
    assert any(check.name == "typescript_parse" and check.status == "passed" for check in result.checks)
    assert not any(check.status == "failed" for check in result.checks)


def test_service_verify_resolves_typescript_node_modules_imports(tmp_path: Path) -> None:
    (tmp_path / "index.ts").write_text('import pkg from "pkg";\nconsole.log(pkg);\n', encoding="utf-8")

    package_root = tmp_path / "node_modules" / "pkg"
    package_root.mkdir(parents=True)
    (package_root / "package.json").write_text('{"name":"pkg","types":"index.d.ts"}', encoding="utf-8")
    (package_root / "index.d.ts").write_text('declare const pkg: string; export default pkg;\n', encoding="utf-8")

    result = RefactorQService().verify(tmp_path)

    assert result.status == "passed"
    assert any(check.name == "typescript_typecheck" and check.status == "passed" for check in result.checks)


def test_service_tui_ignores_gjc_runtime_artifacts(tmp_path: Path) -> None:
    (tmp_path / "sample.py").write_text("import os\n", encoding="utf-8")
    (tmp_path / "sample.ts").write_text("const unusedValue = 1;\n", encoding="utf-8")

    runtime_root = tmp_path / ".gjc" / "_session-1" / "state" / "team" / "worker-1"
    runtime_root.mkdir(parents=True)
    (runtime_root / "sample.py").write_text("import os\n", encoding="utf-8")
    (runtime_root / "sample.ts").write_text("const unusedValue = 1;\n", encoding="utf-8")

    result = RefactorQService().tui_source(tmp_path)

    candidate_files = {path for row in result.candidate_rows for path in row.files}

    assert result.repo.python_files == 1
    assert result.repo.typescript_files == 1
    assert len(result.candidate_rows) == 2
    assert candidate_files == {"sample.py", "sample.ts"}
