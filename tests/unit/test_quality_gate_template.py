"""Regression: the quality-gate cookiecutter template must render into a working gate.

The template once imported ``GateCheckResult``, a name that does not exist, so a
gate generated from it failed on import and was not a ``GatePlugin``.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from bernstein.core.quality.gate_plugins import GatePlugin, GatePluginRegistry
from bernstein.core.quality.gate_runner import GateResult

TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "templates" / "quality_gate_plugin"
_PLACEHOLDER = re.compile(r"\{\{\s*cookiecutter\.(\w+)\s*\}\}")


def _render(text: str, context: dict[str, str]) -> str:
    return _PLACEHOLDER.sub(lambda match: context[match.group(1)], text)


def _render_template(dest: Path, overrides: dict[str, str]) -> tuple[dict[str, str], Path]:
    """Render the cookiecutter template into ``dest`` by substituting its placeholders."""
    context: dict[str, str] = {
        **json.loads((TEMPLATE_DIR / "cookiecutter.json").read_text(encoding="utf-8")),
        **overrides,
    }
    (source_dir,) = [path for path in TEMPLATE_DIR.iterdir() if path.is_dir()]
    out_dir = dest / _render(source_dir.name, context)
    out_dir.mkdir(parents=True)
    for source in source_dir.iterdir():
        rendered = _render(source.read_text(encoding="utf-8"), context)
        (out_dir / _render(source.name, context)).write_text(rendered, encoding="utf-8")
    return context, out_dir


@pytest.fixture(
    params=[
        pytest.param({}, id="defaults"),
        pytest.param({"gate_name": "no_todo_gate", "gate_class": "NoTodoGate"}, id="custom-names"),
    ]
)
def rendered(request: pytest.FixtureRequest, tmp_path: Path) -> tuple[dict[str, str], Path]:
    return _render_template(tmp_path / "generated", request.param)


def _import_generated(context: dict[str, str], out_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    module_path = out_dir / f"{context['gate_name']}.py"
    spec = importlib.util.spec_from_file_location(f"generated_{context['gate_name']}", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # A dataclass under ``from __future__ import annotations`` looks its module up here.
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def test_render_leaves_no_placeholders(rendered: tuple[dict[str, str], Path]) -> None:
    _, out_dir = rendered

    for path in out_dir.iterdir():
        assert "cookiecutter" not in path.read_text(encoding="utf-8"), path.name


def test_generated_gate_imports_and_is_a_gate_plugin(
    rendered: tuple[dict[str, str], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    context, out_dir = rendered

    module = _import_generated(context, out_dir, monkeypatch)
    gate = getattr(module, context["gate_class"])()

    assert isinstance(gate, GatePlugin)
    assert gate.name == context["gate_name"]


def test_generated_gate_runs_on_a_tmp_dir(
    rendered: tuple[dict[str, str], Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context, out_dir = rendered
    run_dir = tmp_path / "work"
    run_dir.mkdir()
    gate = getattr(_import_generated(context, out_dir, monkeypatch), context["gate_class"])()

    result = gate.run(["a.py"], run_dir, "title", "description")

    assert isinstance(result, GateResult)
    assert result.name == context["gate_name"]
    assert result.status == "pass"
    assert result.blocked is False


def test_generated_gate_is_discovered_by_the_registry(rendered: tuple[dict[str, str], Path], tmp_path: Path) -> None:
    context, out_dir = rendered
    workdir = tmp_path / "project"
    gates_dir = workdir / ".bernstein" / "gates"
    gates_dir.mkdir(parents=True)
    module_name = f"{context['gate_name']}.py"
    (gates_dir / module_name).write_text((out_dir / module_name).read_text(encoding="utf-8"), encoding="utf-8")

    plugin = GatePluginRegistry(workdir).get(context["gate_name"])

    assert plugin is not None
    assert plugin.name == context["gate_name"]


def test_generated_tests_pass(rendered: tuple[dict[str, str], Path]) -> None:
    context, out_dir = rendered

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"test_{context['gate_name']}.py"],
        cwd=out_dir,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
