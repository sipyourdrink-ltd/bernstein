"""Unit tests for hook JSON stdin/stdout contract."""

from __future__ import annotations

import json
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

def test_hook_receives_json_stdin(hooks_dir: Path, tmp_path: Path):
    # Script that writes its stdin to a file
    output_file = tmp_path / "received.json"
    script = hooks_dir / "stdin_test.sh"
    _create_script(script, f"#!/bin/sh\ncat > {output_file}")

    pm = PluginManager()
    pm.load_from_workdir(tmp_path)

    args = {"task_id": "t1", "role": "backend", "title": "test"}
    pm.fire_task_created(**args)

    assert output_file.exists()
    received = json.loads(output_file.read_text())
    assert received == args

def test_hook_returns_json_stdout(hooks_dir: Path, tmp_path: Path, caplog):
    import logging
    caplog.set_level(logging.DEBUG)

    # Script that returns valid JSON
    script = hooks_dir / "stdout_test.sh"
    _create_script(script, '#!/bin/sh\necho \'{"status": "ok", "message": "All good"}\'')

    pm = PluginManager()
    pm.load_from_workdir(tmp_path)

    pm.fire_task_created(task_id="t1", role="r", title="t")

    # Success JSON should be parsed silently
    assert "malformed JSON" not in caplog.text

def test_hook_returns_error_json(hooks_dir: Path, tmp_path: Path, caplog):
    import logging
    caplog.set_level(logging.WARNING)

    # Script that returns error JSON but exit code 0 (non-blocking error)
    script = hooks_dir / "error_json.sh"
    _create_script(script, '#!/bin/sh\necho \'{"status": "error", "message": "Something went wrong"}\'')

    pm = PluginManager()
    pm.load_from_workdir(tmp_path)

    pm.fire_task_created(task_id="t1", role="r", title="t")

    assert "reported error: Something went wrong" in caplog.text

def test_hook_blocking_error_with_json(hooks_dir: Path, tmp_path: Path):
    # Script that returns error JSON and exit code 2
    script = hooks_dir / "block_json.sh"
    _create_script(script, '#!/bin/sh\necho \'{"status": "error", "message": "Blocking failure"}\'\nexit 2')

    pm = PluginManager()
    pm.load_from_workdir(tmp_path)

    with pytest.raises(HookBlockingError) as excinfo:
        pm.fire_task_created(task_id="t1", role="r", title="t")

    assert "Blocking failure" in str(excinfo.value)

def test_hook_malformed_json_warning(hooks_dir: Path, tmp_path: Path, caplog):
    import logging
    caplog.set_level(logging.WARNING)

    # Script that returns invalid JSON
    script = hooks_dir / "bad_json.sh"
    _create_script(script, '#!/bin/sh\necho "Not JSON"')

    pm = PluginManager()
    pm.load_from_workdir(tmp_path)

    pm.fire_task_created(task_id="t1", role="r", title="t")

    assert "returned malformed JSON: Not JSON" in caplog.text
