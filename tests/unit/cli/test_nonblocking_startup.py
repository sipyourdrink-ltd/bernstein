"""Process lifetime regressions for optional startup work (#6052, #7845)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "args,expected", [(("--plan-only", "-g", "example"), 0), (("--dry-run",), 0), (("--plan-only",), 1)]
)
def test_short_lived_command_exits_without_cosmetic_discovery(
    tmp_path: Path, args: tuple[str, ...], expected: int
) -> None:
    # A discarded executor worker used to keep Python alive even after
    # Click had returned from the callback. The child must exit naturally.
    script = """
import sys, time
from unittest.mock import patch
from bernstein.cli import main
def slow_discovery(*args, **kwargs):
    from pathlib import Path
    Path("cosmetic-probe").touch()
    time.sleep(60)
with patch("bernstein.core.agent_discovery.discover_agents_cached", slow_discovery):
    main.cli.main(args=sys.argv[1:])
"""
    env = dict(os.environ, BERNSTEIN_NO_SPLASH="1")
    source = Path(__file__).resolve().parents[3] / "src"
    env["PYTHONPATH"] = str(source)
    result = subprocess.run(
        [sys.executable, "-c", script, *args],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=25,
    )
    assert result.returncode == expected, (result.stdout, result.stderr)
    assert not (tmp_path / "cosmetic-probe").exists()


def test_optional_index_exits_with_the_spawner(tmp_path: Path) -> None:
    script = """
import pathlib, threading, time
from bernstein.core.knowledge import rag
started = threading.Event()
def slow_build(workdir):
    started.set()
    time.sleep(60)
rag.build_or_update_index = slow_build
rag.start_background_index(pathlib.Path.cwd())
assert started.wait(5)
print("ready")
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    assert result.stdout.strip() == "ready"


@pytest.mark.parametrize(
    "args,field",
    [
        (["--json", "--dry-run"], "tasks"),
        (["--output", "json", "run", "--dry-run", "--goal", "fixture"], "tasks"),
        (["--json", "run", "--plan-only", "--goal", "fixture"], "plan"),
    ],
)
def test_machine_preview_is_one_json_document(tmp_path: Path, args: list[str], field: str) -> None:
    source = Path(__file__).resolve().parents[3] / "src"
    env = dict(os.environ, HOME=str(tmp_path), PYTHONPATH=str(source))
    result = subprocess.run(
        [sys.executable, "-m", "bernstein", *args], cwd=tmp_path, env=env, capture_output=True, timeout=25, check=True
    )
    assert field in json.loads(result.stdout)
    assert b"\x1b" not in result.stdout + result.stderr
    assert b"collects no telemetry" not in result.stderr
