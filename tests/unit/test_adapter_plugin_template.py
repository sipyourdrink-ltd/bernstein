"""Regression: the adapter cookiecutter template must render into a working adapter (#6976).

The template was written against a ``CLIAdapter`` contract that no longer
exists: it passed arguments to ``CLIAdapter.__init__``, declared a positional
``spawn(task_description, ...)``, and built ``SpawnResult(success=...)``. A
generated adapter raised ``TypeError`` on construction and again on spawn.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from bernstein.core.models import ModelConfig

from bernstein.adapters.base import CLIAdapter, SpawnResult

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_DIR = REPO_ROOT / "templates" / "adapter_plugin"
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
        pytest.param({"adapter_name": "acme_agent", "adapter_class": "AcmeAgentAdapter"}, id="custom-names"),
    ]
)
def rendered(request: pytest.FixtureRequest, tmp_path: Path) -> tuple[dict[str, str], Path]:
    return _render_template(tmp_path / "generated", request.param)


def _adapter_class(context: dict[str, str], out_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    module_path = out_dir / f"{context['adapter_name']}.py"
    spec = importlib.util.spec_from_file_location(f"generated_{context['adapter_name']}", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return getattr(module, context["adapter_class"])


def test_render_leaves_no_placeholders(rendered: tuple[dict[str, str], Path]) -> None:
    _, out_dir = rendered

    for path in out_dir.iterdir():
        assert "cookiecutter" not in path.read_text(encoding="utf-8"), path.name


def test_generated_adapter_constructs_as_the_registry_does(
    rendered: tuple[dict[str, str], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    context, out_dir = rendered
    adapter_class = _adapter_class(context, out_dir, monkeypatch)

    # The registry instantiates an entry-point adapter with no arguments.
    adapter = adapter_class()

    assert isinstance(adapter, CLIAdapter)
    assert not inspect.isabstract(adapter_class)
    assert adapter.name() == context["adapter_class"]


def test_generated_spawn_accepts_every_keyword_the_base_declares(
    rendered: tuple[dict[str, str], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The spawner calls ``spawn`` by keyword, so the generated signature must keep up with the base."""
    context, out_dir = rendered
    adapter_class = _adapter_class(context, out_dir, monkeypatch)

    base = inspect.signature(CLIAdapter.spawn).parameters
    generated = inspect.signature(adapter_class.spawn).parameters

    assert list(generated) == list(base)
    for name, parameter in base.items():
        assert generated[name].kind == parameter.kind, name


def test_generated_spawn_launches_and_returns_a_spawn_result(
    rendered: tuple[dict[str, str], Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context, out_dir = rendered
    adapter = _adapter_class(context, out_dir, monkeypatch)()
    monkeypatch.setattr(
        adapter,
        "build_command",
        lambda prompt, _model: [sys.executable, "-c", f"print({prompt!r})"],
    )
    workdir = tmp_path / "project"
    workdir.mkdir()

    result = adapter.spawn(
        prompt="write the report",
        workdir=workdir,
        model_config=ModelConfig(model="test-model", effort="medium"),
        session_id="backend-abc123",
        mcp_config=None,
        timeout_seconds=0,
        task_scope="medium",
        budget_multiplier=1.0,
        system_addendum="",
    )

    assert isinstance(result, SpawnResult)
    assert result.pid > 0
    assert result.log_path == workdir / ".sdd" / "runtime" / "backend-abc123.log"
    assert result.timeout_timer is None
    proc = result.proc
    assert isinstance(proc, subprocess.Popen)
    assert proc.wait(timeout=60) == 0
    assert "write the report" in result.log_path.read_text(encoding="utf-8")


def test_generated_build_command_carries_prompt_and_model(
    rendered: tuple[dict[str, str], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    context, out_dir = rendered
    adapter = _adapter_class(context, out_dir, monkeypatch)()

    cmd = adapter.build_command("fix it", ModelConfig(model="test-model", effort="medium"))

    assert cmd[0] == context["adapter_name"]
    assert "fix it" in cmd
    assert cmd[cmd.index("--model") + 1] == "test-model"


def test_generated_test_file_passes(rendered: tuple[dict[str, str], Path]) -> None:
    """The test file a user gets must pass against the adapter they get."""
    context, out_dir = rendered

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            f"test_{context['adapter_name']}.py",
            "-q",
            "-p",
            "no:cacheprovider",
            "--rootdir",
            str(out_dir),
            "-c",
            "/dev/null" if sys.platform != "win32" else "NUL",
        ],
        cwd=out_dir,
        capture_output=True,
        text=True,
        timeout=300,
        env=_subprocess_env(out_dir),
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "4 passed" in completed.stdout


def _subprocess_env(out_dir: Path) -> dict[str, str]:
    """Environment for the generated suite: this checkout's sources, plus the rendered directory."""
    import os

    env = dict(os.environ)
    paths = [str(out_dir), str(REPO_ROOT / "src")]
    existing = env.get("PYTHONPATH")
    if existing:
        paths.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(paths)
    return env
