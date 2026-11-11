"""The bundled example quality-gate plugin is a real, blocking ``GatePlugin``."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from bernstein.core.quality.gate_plugins import GatePlugin, GatePluginRegistry
from bernstein.core.quality.gate_runner import GateResult

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
def test_failing_scan_blocks(tmp_path: Path, command: str) -> None:
    result = _gate(command).run([], tmp_path, "title", "description")

    assert result.status == "fail"
    assert result.blocked is True
    assert (tmp_path / ".sdd" / "metrics" / "custom_gates.jsonl").is_file()


def test_timeout_blocks(tmp_path: Path) -> None:
    result = _gate(f'"{sys.executable}" -c "import time; time.sleep(5)"', timeout_s=1).run([], tmp_path, "t", "d")

    assert result.status == "fail"
    assert result.blocked is True
    assert "Timed out" in result.details


def test_registry_discovers_example_from_gates_dir(tmp_path: Path) -> None:
    gates_dir = tmp_path / ".bernstein" / "gates"
    gates_dir.mkdir(parents=True)
    (gates_dir / "security_scan_gate.py").write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")

    plugin = GatePluginRegistry(tmp_path).get("security_scan_cmd")

    assert plugin is not None
