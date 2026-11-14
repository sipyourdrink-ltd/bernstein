"""Render the adapter template and exercise the real CLIAdapter contract."""

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

from bernstein.adapters.base import CLIAdapter

TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "templates" / "adapter_plugin"
_PLACEHOLDER = re.compile(r"\{\{\s*cookiecutter\.(\w+)\s*\}\}")


@pytest.fixture(params=[{}, {"adapter_name": "custom_agent", "adapter_class": "CustomAgentAdapter"}])
def rendered(request: pytest.FixtureRequest, tmp_path: Path) -> tuple[dict[str, str], Path]:
    context = {**json.loads((TEMPLATE_DIR / "cookiecutter.json").read_text()), **request.param}

    def render(text: str) -> str:
        return _PLACEHOLDER.sub(lambda match: context[match.group(1)], text)

    (source_dir,) = [path for path in TEMPLATE_DIR.iterdir() if path.is_dir()]
    out_dir = tmp_path / render(source_dir.name)
    out_dir.mkdir()
    for source in source_dir.iterdir():
        (out_dir / render(source.name)).write_text(render(source.read_text()), encoding="utf-8")
    return context, out_dir


def _adapter_class(rendered: tuple[dict[str, str], Path]) -> Any:
    context, out_dir = rendered
    spec = importlib.util.spec_from_file_location("generated_adapter", out_dir / f"{context['adapter_name']}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, context["adapter_class"])


def test_generated_adapter_constructs(rendered: tuple[dict[str, str], Path]) -> None:
    adapter = _adapter_class(rendered)()

    assert isinstance(adapter, CLIAdapter)
    assert adapter.name() == rendered[0]["adapter_name"]
    assert adapter.rate_limit_meter.adapter_name == adapter.name()


def test_generated_spawn_signature_matches_base(rendered: tuple[dict[str, str], Path]) -> None:
    assert inspect.signature(_adapter_class(rendered).spawn) == inspect.signature(CLIAdapter.spawn)


@pytest.mark.parametrize("exit_code", [0, 7])
def test_generated_adapter_launches_without_waiting(
    rendered: tuple[dict[str, str], Path], tmp_path: Path, exit_code: int
) -> None:
    from bernstein.core.models import ModelConfig

    cli = tmp_path / "fake-agent"
    cli.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys, time\n"
        "for _ in range(500):\n"
        "    if pathlib.Path('release').exists():\n"
        "        break\n"
        "    time.sleep(0.01)\n"
        "else:\n"
        "    sys.exit(99)\n"
        "print(json.dumps(sys.argv[1:]), flush=True)\n"
        "print('agent stderr', file=sys.stderr, flush=True)\n"
        f"sys.exit({exit_code})\n",
        encoding="utf-8",
    )
    cli.chmod(0o700)
    adapter = _adapter_class(rendered)(cli_path=str(cli))

    result = adapter.spawn(
        prompt="Test task",
        workdir=tmp_path,
        model_config=ModelConfig(model="test-model", effort="high"),
        session_id="real-child",
        timeout_seconds=0,
        system_addendum="Follow the protocol",
    )

    try:
        assert result.proc is not None
        assert result.pid == result.proc.pid > 0
        assert result.proc.poll() is None
    finally:
        (tmp_path / "release").touch()
        result.proc.wait(timeout=5)

    assert result.proc.returncode == exit_code
    lines = result.log_path.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0]) == [
        "run",
        "--description",
        "Test task\n\n## System Addendum\nFollow the protocol",
        "--model",
        "test-model",
        "--effort",
        "high",
    ]
    assert lines[1] == "agent stderr"
    notice = json.loads((tmp_path / ".sdd/runtime/capability_notices.jsonl").read_text())
    assert notice["delivered"] is True
    assert notice["channel"] == "prompt-append"


def test_generated_tests_pass(rendered: tuple[dict[str, str], Path]) -> None:
    context, out_dir = rendered
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"test_{context['adapter_name']}.py"],
        cwd=out_dir,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
