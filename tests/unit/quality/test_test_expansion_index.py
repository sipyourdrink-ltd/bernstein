"""Production-path import-index regressions; all source fixtures are parsed only."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from bernstein.core.models import Task

from bernstein.core.quality.gate_pipeline import GatePipelineStep, GateResult
from bernstein.core.quality.gate_runner import GateRunner
from bernstein.core.quality.quality_gates import QualityGatesConfig


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _run(root: Path, changed: list[str]) -> GateResult:
    step = GatePipelineStep(name="test_expansion", required=True)
    runner = GateRunner(QualityGatesConfig(pipeline=[step], cache_enabled=True), root)
    task = Task(id="index-test", title="index", description="local AST fixtures", role="qa")
    return asyncio.run(runner.run_gate(step, task, root, changed))


def _layout(root: Path) -> list[str]:
    _write(
        root,
        {
            "src/demo/__init__.py": "",
            "src/demo/feature.py": "TARGET_INDEX_MARKER = 15\n",
            "src/demo/bridge.py": "from demo.feature import TARGET_INDEX_MARKER\n",
            "tests/test_feature.py": "import demo.bridge\ndef test_placeholder():\n    assert True\n",
        },
    )
    return ["src/demo/feature.py", "tests/test_feature.py"]


def test_warm_gate_reuses_the_graph_without_reusing_a_verdict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import bernstein.core.quality.test_impact as impact

    changed = _layout(tmp_path)
    parsed: list[str] = []
    original = impact.ast.parse

    def record(source: str, *args: object, **kwargs: object):
        parsed.append(source)
        return original(source, *args, **kwargs)

    monkeypatch.setattr(impact.ast, "parse", record)
    first = _run(tmp_path, changed)
    assert first.status == "pass" and not first.cached
    parsed.clear()
    second = _run(tmp_path, changed)
    assert second.status == "pass" and not second.cached
    assert "TARGET_INDEX_MARKER = 15\n" not in parsed


@pytest.mark.parametrize(
    "directory", [".venv", "node_modules", "build", "dist", "__pycache__", ".hidden", "venv", "custom-runtime"]
)
def test_flat_project_never_parses_vendored_or_generated_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, directory: str
) -> None:
    import bernstein.core.quality.test_impact as impact

    _write(
        tmp_path,
        {
            "feature.py": "VALUE = 1\n",
            "feature_test.py": "import feature\n",
            f"{directory}/nested/vendor.py": "VENDOR_INDEX_MARKER = 99\n",
        },
    )
    if directory in {"venv", "custom-runtime"}:
        (tmp_path / directory / "pyvenv.cfg").write_text("include-system-site-packages = false\n")
    parsed: list[str] = []
    original = impact.ast.parse

    def record(source: str, *args: object, **kwargs: object):
        parsed.append(source)
        return original(source, *args, **kwargs)

    monkeypatch.setattr(impact.ast, "parse", record)
    assert _run(tmp_path, ["feature.py", "feature_test.py"]).status == "pass"
    assert "VENDOR_INDEX_MARKER = 99\n" not in parsed


def test_intermediate_import_changes_invalidate_a_warm_graph(tmp_path: Path) -> None:
    changed = _layout(tmp_path)
    assert _run(tmp_path, changed).status == "pass"
    (tmp_path / "src/demo/bridge.py").write_text("UNRELATED = 1\n")
    result = _run(tmp_path, changed)
    assert result.status == "fail" and result.blocked and not result.cached
    assert result.metadata["uncovered_files"] == ["src/demo/feature.py"]


def test_new_and_deleted_child_modules_invalidate_package_import_edges(tmp_path: Path) -> None:
    changed = _layout(tmp_path)
    (tmp_path / "src/demo/bridge.py").write_text("from demo import child\n")
    assert _run(tmp_path, changed).status == "fail"
    child = tmp_path / "src/demo/child.py"
    child.write_text("from demo.feature import TARGET_INDEX_MARKER\n")
    assert _run(tmp_path, changed).status == "pass"
    child.unlink()
    assert _run(tmp_path, changed).status == "fail"


def test_root_bridge_edits_and_deletion_invalidate_a_warm_graph(tmp_path: Path) -> None:
    changed = _layout(tmp_path)
    _write(tmp_path, {"scripts/bridge.py": "import demo.feature\n", "tests/test_feature.py": "import scripts.bridge\n"})
    assert _run(tmp_path, changed).status == "pass"
    bridge = tmp_path / "scripts/bridge.py"
    bridge.write_text("UNRELATED = 1\n")
    assert _run(tmp_path, changed).status == "fail"
    bridge.write_text("import demo.feature\n")
    assert _run(tmp_path, changed).status == "pass"
    bridge.unlink()
    assert _run(tmp_path, changed).status == "fail"


def test_changed_test_imports_are_content_validated(tmp_path: Path) -> None:
    changed = _layout(tmp_path)
    assert _run(tmp_path, changed).status == "pass"
    (tmp_path / "tests/test_feature.py").write_text("def test_placeholder():\n    assert True\n")
    assert _run(tmp_path, changed).status == "fail"


def test_distinct_repositories_do_not_share_import_edges(tmp_path: Path) -> None:
    left, right = tmp_path / "left", tmp_path / "right"
    changed = _layout(left)
    _layout(right)
    (right / "src/demo/bridge.py").write_text("UNRELATED = 1\n")
    assert _run(left, changed).status == "pass"
    assert _run(right, changed).status == "fail"


def test_indexing_never_overwrites_the_cli_cache(tmp_path: Path) -> None:
    changed = _layout(tmp_path)
    cache = tmp_path / ".sdd/test_deps.json"
    cache.parent.mkdir()
    original = b'{"not": "a compatibility map"}\n'
    cache.write_bytes(original)
    assert _run(tmp_path, changed).status == "pass"
    assert cache.read_bytes() == original


def test_module_reachability_does_not_claim_symbol_execution(tmp_path: Path) -> None:
    changed = _layout(tmp_path)
    result = _run(tmp_path, changed)
    # The placeholder imports the bridge and never reads the target symbol.
    assert result.status == "pass"
    assert "module(s) reachable" in result.details
