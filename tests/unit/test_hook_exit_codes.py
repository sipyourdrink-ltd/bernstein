"""Unit tests for hook exit code semantics."""

from __future__ import annotations

import logging
import stat
from pathlib import Path

import pytest

from bernstein.plugins.manager import HookBlockingError, PluginManager


@pytest.fixture
def hooks_dir(tmp_path: Path) -> Path:
    d = tmp_path / ".bernstein" / "hooks" / "on_task_created"
    d.mkdir(parents=True)
    return d

def _create_script(path: Path, content: str):
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)

def test_exit_code_0_success(hooks_dir: Path, caplog):
    caplog.set_level(logging.DEBUG)
    script = hooks_dir / "success.sh"
    _create_script(script, "#!/bin/sh\nexit 0")

    pm = PluginManager()
    pm.load_from_workdir(hooks_dir.parent.parent.parent)

    pm.fire_task_created(task_id="t1", role="r", title="t")

    # Should not log any warnings or errors
    assert "exited with code" not in caplog.text

def test_exit_code_2_blocks(hooks_dir: Path):
    script = hooks_dir / "block.sh"
    _create_script(script, "#!/bin/sh\necho 'Stop right there!' >&2\nexit 2")

    pm = PluginManager()
    pm.load_from_workdir(hooks_dir.parent.parent.parent)

    with pytest.raises(HookBlockingError) as excinfo:
        pm.fire_task_created(task_id="t1", role="r", title="t")

    assert "Stop right there!" in str(excinfo.value)
    assert "on_task_created" in str(excinfo.value)

def test_exit_code_1_warns(hooks_dir: Path, caplog):
    caplog.set_level(logging.WARNING)
    script = hooks_dir / "warn.sh"
    _create_script(script, "#!/bin/sh\necho 'Just a warning' >&2\nexit 1")

    pm = PluginManager()
    pm.load_from_workdir(hooks_dir.parent.parent.parent)

    # Should not raise
    pm.fire_task_created(task_id="t1", role="r", title="t")

    assert "exited with code 1" in caplog.text
    assert "Just a warning" in caplog.text

def test_other_exit_code_warns(hooks_dir: Path, caplog):
    caplog.set_level(logging.WARNING)
    script = hooks_dir / "other.sh"
    _create_script(script, "#!/bin/sh\nexit 42")

    pm = PluginManager()
    pm.load_from_workdir(hooks_dir.parent.parent.parent)

    # Should not raise
    pm.fire_task_created(task_id="t1", role="r", title="t")

    assert "exited with code 42" in caplog.text
