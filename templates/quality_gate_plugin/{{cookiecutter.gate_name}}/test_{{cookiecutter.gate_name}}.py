"""Tests for {{ cookiecutter.gate_name }} quality gate."""

from __future__ import annotations

import shutil
from pathlib import Path

import {{ cookiecutter.gate_name }} as gate_module
import pytest
from {{ cookiecutter.gate_name }} import {{ cookiecutter.gate_class }}, {{ cookiecutter.gate_class }}Config

from bernstein.core.quality.gate_plugins import GatePlugin, GatePluginRegistry


def _run(gate: {{ cookiecutter.gate_class }}, run_dir: Path):
    return gate.run(["src/example.py"], run_dir, "task title", "task description")


class Fixed{{ cookiecutter.gate_class }}({{ cookiecutter.gate_class }}):
    """Gate whose findings are fixed, so each verdict can be exercised."""

    def __init__(self, errors: int, warnings: int, config: {{ cookiecutter.gate_class }}Config | None = None) -> None:
        super().__init__(config)
        self._errors = errors
        self._warnings = warnings

    def scan(self, changed_files: list[str], run_dir: Path) -> tuple[int, int]:
        return self._errors, self._warnings


class Broken{{ cookiecutter.gate_class }}({{ cookiecutter.gate_class }}):
    """Gate whose scan raises."""

    def scan(self, changed_files: list[str], run_dir: Path) -> tuple[int, int]:
        raise RuntimeError("tool crashed")


class Test{{ cookiecutter.gate_class }}:
    """Test suite for {{ cookiecutter.gate_name }} quality gate."""

    def test_is_a_gate_plugin(self) -> None:
        gate = {{ cookiecutter.gate_class }}()

        assert isinstance(gate, GatePlugin)
        assert gate.name == "{{ cookiecutter.gate_name }}"

    def test_gate_disabled(self, tmp_path: Path) -> None:
        gate = {{ cookiecutter.gate_class }}({{ cookiecutter.gate_class }}Config(enabled=False))

        result = _run(gate, tmp_path)

        assert result.status == "skipped"
        assert result.blocked is False

    def test_gate_pass(self, tmp_path: Path) -> None:
        # TODO: Set up test fixtures for a passing scenario
        result = _run({{ cookiecutter.gate_class }}(), tmp_path)

        assert result.name == "{{ cookiecutter.gate_name }}"
        assert result.status == "pass"
        assert result.blocked is False

    def test_gate_fail_leaves_blocking_to_runner(self, tmp_path: Path) -> None:
        # TODO: Replace the fixed findings with a real failing scenario
        result = _run(Fixed{{ cookiecutter.gate_class }}(errors=1, warnings=0), tmp_path)

        assert result.status == "fail"
        assert result.blocked is False
        assert result.required is True

    def test_gate_threshold(self, tmp_path: Path) -> None:
        config = {{ cookiecutter.gate_class }}Config(threshold=5)

        within = _run(Fixed{{ cookiecutter.gate_class }}(errors=5, warnings=0, config=config), tmp_path)
        above = _run(Fixed{{ cookiecutter.gate_class }}(errors=6, warnings=0, config=config), tmp_path)

        assert within.status == "pass"
        assert above.status == "fail"
        assert above.blocked is False

    def test_warnings_fail_only_when_configured(self, tmp_path: Path) -> None:
        lenient = Fixed{{ cookiecutter.gate_class }}(errors=0, warnings=2)
        config = {{ cookiecutter.gate_class }}Config(fail_on="warning")
        strict = Fixed{{ cookiecutter.gate_class }}(errors=0, warnings=2, config=config)

        assert _run(lenient, tmp_path).status == "pass"
        assert _run(strict, tmp_path).status == "fail"
        assert _run(strict, tmp_path).blocked is False

    def test_scan_error_propagates_to_runner(self, tmp_path: Path) -> None:
        with pytest.raises(RuntimeError, match="tool crashed"):
            _run(Broken{{ cookiecutter.gate_class }}(), tmp_path)

    def test_registry_discovers_gate_from_file(self, tmp_path: Path) -> None:
        gates_dir = tmp_path / ".bernstein" / "gates"
        gates_dir.mkdir(parents=True)
        shutil.copy(Path(gate_module.__file__), gates_dir / "{{ cookiecutter.gate_name }}.py")

        plugin = GatePluginRegistry(tmp_path).get("{{ cookiecutter.gate_name }}")

        assert plugin is not None
        assert plugin.name == "{{ cookiecutter.gate_name }}"
