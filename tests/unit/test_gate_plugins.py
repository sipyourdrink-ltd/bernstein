"""Unit tests for quality-gate plugin discovery and validation."""

from __future__ import annotations

import textwrap
from pathlib import Path

import bernstein.core.gate_plugins as gate_plugins_module
import pytest
from bernstein.core.gate_plugins import GatePlugin, GatePluginRegistry


def test_registry_discovers_file_plugin(tmp_path: Path) -> None:
    gates_dir = tmp_path / ".bernstein" / "gates"
    gates_dir.mkdir(parents=True)
    (gates_dir / "custom_gate.py").write_text(
        textwrap.dedent(
            """
            from bernstein.core.gate_plugins import GatePlugin
            from bernstein.core.gate_runner import GateResult


            class CustomGate(GatePlugin):
                @property
                def name(self) -> str:
                    return "custom_gate"

                def run(self, changed_files, run_dir, task_title, task_description):
                    return GateResult(
                        name=self.name,
                        status="pass",
                        required=False,
                        blocked=False,
                        cached=False,
                        duration_ms=0,
                        details="ok",
                    )
            """
        ),
        encoding="utf-8",
    )

    registry = GatePluginRegistry(tmp_path)

    plugin = registry.get("custom_gate")

    assert plugin is not None
    assert plugin.name == "custom_gate"
    assert [item.name for item in registry.all_plugins()] == ["custom_gate"]


def test_registry_rejects_builtin_name_collision_from_discovery(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    gates_dir = tmp_path / ".bernstein" / "gates"
    gates_dir.mkdir(parents=True)
    (gates_dir / "lint.py").write_text(
        textwrap.dedent(
            """
            from bernstein.core.gate_plugins import GatePlugin
            from bernstein.core.gate_runner import GateResult


            class LintGate(GatePlugin):
                @property
                def name(self) -> str:
                    return "lint"

                def run(self, changed_files, run_dir, task_title, task_description):
                    return GateResult(
                        name=self.name,
                        status="pass",
                        required=True,
                        blocked=False,
                        cached=False,
                        duration_ms=0,
                        details="ok",
                    )
            """
        ),
        encoding="utf-8",
    )

    registry = GatePluginRegistry(tmp_path, built_in_names={"lint"})

    assert registry.get("lint") is None
    assert "collides with a built-in gate" in caplog.text


def test_register_rejects_duplicate_names(tmp_path: Path) -> None:
    class DemoGate(GatePlugin):
        @property
        def name(self) -> str:
            return "demo"

        def run(self, changed_files: list[str], run_dir: Path, task_title: str, task_description: str):  # type: ignore[override]
            raise AssertionError("not used")

    registry = GatePluginRegistry(tmp_path)
    registry.register(DemoGate())

    with pytest.raises(ValueError, match="Duplicate gate plugin name"):
        registry.register(DemoGate())


def test_discovery_skips_invalid_plugin_module(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    gates_dir = tmp_path / ".bernstein" / "gates"
    gates_dir.mkdir(parents=True)
    (gates_dir / "broken.py").write_text("def this is not valid python\n", encoding="utf-8")

    registry = GatePluginRegistry(tmp_path)

    assert registry.all_plugins() == []
    assert "Failed to load gate plugin" in caplog.text


def test_registry_loads_entrypoint_plugin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class EntryPointGate(GatePlugin):
        @property
        def name(self) -> str:
            return "entrypoint_gate"

        def run(self, changed_files: list[str], run_dir: Path, task_title: str, task_description: str):  # type: ignore[override]
            raise AssertionError("not used")

    class FakeEntryPoint:
        name = "entrypoint_gate"

        def load(self) -> type[GatePlugin]:
            return EntryPointGate

    def _fake_entry_points(*, group: str) -> list[object]:
        assert group == "bernstein.gates"
        return [FakeEntryPoint()]

    monkeypatch.setattr(gate_plugins_module, "entry_points", _fake_entry_points)
    registry = GatePluginRegistry(tmp_path)

    plugin = registry.get("entrypoint_gate")

    assert plugin is not None
    assert plugin.name == "entrypoint_gate"
