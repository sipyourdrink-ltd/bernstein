"""Timeout probes must not interrupt imports in concurrent test processes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SOURCE = Path("src/bernstein/core/lineage/tips.py")
_PROBE_PLUGIN = r"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    root = Path(os.environ["MUTATION_PROBE_REPO"])
    source = root / "src/bernstein/core/lineage/tips.py"
    original = source.read_bytes()
    original_open = Path.open
    observation = {}

    def observe_open(path, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        watch = path.name == "tips.py" and mode == "w" and not observation
        before = path.read_bytes() if watch else b""
        handle = original_open(path, *args, **kwargs)
        if watch:
            observation["target"] = str(path.resolve())
            observation["truncated_size"] = len(path.read_bytes())
            observation["checkout_unchanged_during_write"] = source.read_bytes() == original
            reader = subprocess.Popen(
                [sys.executable, "-m", "pytest", "-q", "tests/unit/test_notifications.py"],
                cwd=root,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            output, _ = reader.communicate(timeout=45)
            observation["reader_exit"] = reader.returncode
            observation["reader_output"] = output
            observation["before"] = before.decode("utf-8")
        return handle

    Path.open = observe_open
    try:
        yield
    finally:
        Path.open = original_open
        if observation:
            target = Path(observation["target"])
            observation["target_restored"] = target.read_text() == observation.pop("before")
        observation["checkout_unchanged_after_probe"] = source.read_bytes() == original
        Path(os.environ["MUTATION_PROBE_REPORT"]).write_text(json.dumps(observation))
"""


@pytest.mark.parametrize(
    "probe",
    ["test_timed_out_mutant_is_not_scored_as_kill", "test_material_timeouts_mark_module_unmeasured"],
)
def test_timeout_probe_preserves_concurrent_production_imports(tmp_path: Path, probe: str) -> None:
    """Pause the real probe's write while a separate notifications suite imports."""
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    (plugin_dir / "mutation_write_observer.py").write_text(_PROBE_PLUGIN)
    report = tmp_path / "observation.json"
    env = os.environ.copy()
    env.update(
        MUTATION_PROBE_REPO=str(_ROOT),
        MUTATION_PROBE_REPORT=str(report),
        PYTHONPATH=os.pathsep.join((str(_ROOT / "src"), str(_ROOT), str(plugin_dir))),
    )
    original = (_ROOT / _SOURCE).read_bytes()
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "mutation_write_observer",
            "--basetemp",
            str(tmp_path / "probe-tmp"),
            f"tests/unit/test_mutmut_critical_mutant_timeout.py::{probe}",
        ],
        cwd=_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    observation = json.loads(report.read_text())
    assert observation["reader_exit"] == 0, observation
    assert Path(observation["target"]).is_relative_to(tmp_path / "probe-tmp"), observation
    assert observation["truncated_size"] == 0, observation
    assert observation["checkout_unchanged_during_write"], observation
    assert observation["checkout_unchanged_after_probe"], observation
    assert observation["target_restored"], observation
    assert (_ROOT / _SOURCE).read_bytes() == original
