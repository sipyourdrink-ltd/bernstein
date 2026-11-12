"""The bundled example quality-gate plugin is a real ``GatePlugin`` governed by the pipeline."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest
from bernstein.core.models import Task

from bernstein.core.quality.gate_pipeline import GatePipelineStep
from bernstein.core.quality.gate_plugins import GatePlugin, GatePluginRegistry
from bernstein.core.quality.gate_runner import GateResult, GateRunner
from bernstein.core.quality.quality_gates import QualityGatesConfig, run_quality_gates

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "plugins" / "quality_gate_plugin.py"


def _load_example() -> Any:
    spec = importlib.util.spec_from_file_location("example_quality_gate_plugin", EXAMPLE)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _gate(command: str, **kwargs: Any) -> GatePlugin:
    return _load_example().SecurityScanGate(command=command, **kwargs)


def test_passing_scan_does_not_block(tmp_path: Path) -> None:
    result = _gate(f'"{sys.executable}" -c "print(1)"').run([], tmp_path, "title", "description")

    assert isinstance(result, GateResult)
    assert result.status == "pass"
    assert result.blocked is False


@pytest.mark.parametrize(
    "command",
    [f'"{sys.executable}" -c "raise SystemExit(1)"', "definitely-not-a-command-6962"],
)
def test_failing_scan_leaves_blocking_to_runner(tmp_path: Path, command: str) -> None:
    result = _gate(command).run([], tmp_path, "title", "description")

    assert result.status == "fail"
    assert result.blocked is False


def test_timeout_leaves_blocking_to_runner(tmp_path: Path) -> None:
    result = _gate(f'"{sys.executable}" -c "import time; time.sleep(5)"', timeout_s=1).run([], tmp_path, "t", "d")

    assert result.status == "fail"
    assert result.blocked is False
    assert "Timed out" in result.details


def test_registry_discovers_example_from_gates_dir(tmp_path: Path) -> None:
    gates_dir = tmp_path / ".bernstein" / "gates"
    gates_dir.mkdir(parents=True)
    (gates_dir / "security_scan_gate.py").write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")

    plugin = GatePluginRegistry(tmp_path).get("security_scan_cmd")

    assert plugin is not None


@pytest.mark.parametrize("required", [True, False], ids=["required", "advisory"])
def test_example_failure_follows_pipeline_required(tmp_path: Path, required: bool) -> None:
    gate = _gate(f'"{sys.executable}" -c "raise SystemExit(1)"')
    step = GatePipelineStep(name=gate.name, required=required, condition="always")
    runner = GateRunner(QualityGatesConfig(pipeline=[step], cache_enabled=False), tmp_path)
    runner._plugin_registry().register(gate)
    task = Task(id="example-failure", title="title", description="description", role="backend")

    report = asyncio.run(runner.run_all(task, tmp_path))

    assert report.results[0].status == "fail"
    assert report.results[0].blocked is required
    assert report.overall_pass is (not required)


def test_scan_record_survives_worktree_removal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    worktree = tmp_path / "agent-worktree"
    worktree.mkdir()
    (worktree / "scanned.txt").write_text("scan evidence", encoding="utf-8")
    command = f'"{sys.executable}" -c "from pathlib import Path; print(Path(\'scanned.txt\').read_text())"'
    gates_dir = project / ".bernstein" / "gates"
    gates_dir.mkdir(parents=True)
    (gates_dir / "security_scan_gate.py").write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    step = GatePipelineStep(name="security_scan_cmd", required=True, condition="always")
    monkeypatch.setenv("BERNSTEIN_SECURITY_CMD", command)
    config = QualityGatesConfig(pipeline=[step], cache_enabled=False)
    task = Task(id="persisted-scan", title="title", description="description", role="backend")

    run_quality_gates(task, worktree, project, config)
    assert not (worktree / ".sdd").exists()
    shutil.rmtree(worktree)

    record = json.loads((project / ".sdd" / "runtime" / "gates" / f"{task.id}.json").read_text())
    result = record["results"][0]
    assert result["name"] == "security_scan_cmd"
    assert result["status"] == "pass"
    assert result["details"] == "scan evidence"
    assert result["metadata"]["command"] == command
    metric = json.loads((project / ".sdd" / "metrics" / "quality_gates.jsonl").read_text())
    assert metric["task_id"] == task.id
    assert metric["gate"] == "security_scan_cmd"
    assert metric["status"] == "pass"
    assert metric["command"] == command
