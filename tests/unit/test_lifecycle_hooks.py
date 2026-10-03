"""Unit tests for the lifecycle-hooks subsystem."""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.hooks_cmd import hooks as hooks_group
from bernstein.core.config.hook_config import (
    PluginHookEntry,
    ScriptHookEntry,
    apply_config,
    parse_hook_config,
)
from bernstein.core.lifecycle.hooks import (
    HookFailure,
    HookRegistry,
    LifecycleContext,
    LifecycleEvent,
)
from bernstein.core.lifecycle.pluggy_bridge import (
    apply_hooks_to_existing_system,
)
from bernstein.plugins import hookimpl


def _write_script(path: Path, body: str) -> Path:
    """Create an executable script with the given body."""
    path.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def test_register_script_runs_with_env_vars(tmp_path: Path) -> None:
    marker = tmp_path / "out.txt"
    script = _write_script(
        tmp_path / "hook.sh",
        f'printf "%s|%s|%s|%s" "$BERNSTEIN_EVENT" "$BERNSTEIN_TASK_ID" "$BERNSTEIN_SESSION_ID" "$BERNSTEIN_WORKDIR" > {marker}\n',
    )

    registry = HookRegistry()
    registry.register_script(LifecycleEvent.PRE_TASK, script)
    ctx = LifecycleContext(
        event=LifecycleEvent.PRE_TASK,
        task="T-1",
        session_id="S-1",
        workdir=tmp_path,
    )
    registry.run(LifecycleEvent.PRE_TASK, ctx)

    assert marker.read_text(encoding="utf-8") == f"pre_task|T-1|S-1|{tmp_path}"


def test_script_nonzero_exit_raises_hook_failure(tmp_path: Path) -> None:
    script = _write_script(tmp_path / "bad.sh", 'echo "boom" >&2\nexit 7\n')

    registry = HookRegistry()
    registry.register_script(LifecycleEvent.POST_TASK, script)
    ctx = LifecycleContext(event=LifecycleEvent.POST_TASK, workdir=tmp_path)

    with pytest.raises(HookFailure) as excinfo:
        registry.run(LifecycleEvent.POST_TASK, ctx)

    assert excinfo.value.exit_code == 7
    assert str(script) in excinfo.value.hook
    assert "boom" in excinfo.value.stderr


def test_register_callable_receives_context(tmp_path: Path) -> None:
    received: list[LifecycleContext] = []

    def handler(ctx: LifecycleContext) -> None:
        received.append(ctx)

    registry = HookRegistry()
    registry.register_callable(LifecycleEvent.PRE_MERGE, handler)
    ctx = LifecycleContext(event=LifecycleEvent.PRE_MERGE, task="T-9", workdir=tmp_path)
    registry.run(LifecycleEvent.PRE_MERGE, ctx)

    assert len(received) == 1
    assert received[0].task == "T-9"
    assert received[0].event is LifecycleEvent.PRE_MERGE


def test_callable_exception_becomes_hook_failure(tmp_path: Path) -> None:
    def boom(ctx: LifecycleContext) -> None:
        raise RuntimeError("nope")

    registry = HookRegistry()
    registry.register_callable(LifecycleEvent.POST_TASK, boom)
    ctx = LifecycleContext(event=LifecycleEvent.POST_TASK, workdir=tmp_path)

    with pytest.raises(HookFailure) as excinfo:
        registry.run(LifecycleEvent.POST_TASK, ctx)

    assert excinfo.value.exit_code is None
    assert isinstance(excinfo.value.__cause__, RuntimeError)


def test_run_async_does_not_block(tmp_path: Path) -> None:
    def slow(ctx: LifecycleContext) -> None:
        time.sleep(0.25)

    registry = HookRegistry()
    registry.register_callable(LifecycleEvent.POST_MERGE, slow)
    ctx = LifecycleContext(event=LifecycleEvent.POST_MERGE, workdir=tmp_path)

    start = time.monotonic()
    future = registry.run_async(LifecycleEvent.POST_MERGE, ctx)
    elapsed = time.monotonic() - start

    # Scheduling must return essentially immediately.
    assert elapsed < 0.1
    future.result(timeout=5)
    registry.shutdown()


def test_config_parses_mixed_script_and_plugin() -> None:
    raw = {
        "pre_task": ["scripts/pre-flight.sh"],
        "post_merge": [
            {"script": "scripts/notify.sh", "timeout": 10},
            {"plugin": "bernstein_plugin_jira"},
        ],
    }

    config = parse_hook_config(raw)

    pre_task_scripts = config.scripts[LifecycleEvent.PRE_TASK]
    assert pre_task_scripts == [ScriptHookEntry(path=Path("scripts/pre-flight.sh"))]

    post_merge_scripts = config.scripts[LifecycleEvent.POST_MERGE]
    assert post_merge_scripts == [ScriptHookEntry(path=Path("scripts/notify.sh"), timeout=10)]

    post_merge_plugins = config.plugins[LifecycleEvent.POST_MERGE]
    assert post_merge_plugins == [PluginHookEntry(name="bernstein_plugin_jira")]


def test_config_rejects_unknown_event() -> None:
    with pytest.raises(Exception) as excinfo:
        parse_hook_config({"totally_fake": ["x.sh"]})
    assert "totally_fake" in str(excinfo.value)


def test_apply_config_registers_scripts_in_order(tmp_path: Path) -> None:
    first = _write_script(tmp_path / "a.sh", "true\n")
    second = _write_script(tmp_path / "b.sh", "true\n")
    config = parse_hook_config({"pre_task": [str(first), str(second)]})

    registry = HookRegistry()
    apply_config(registry, config)

    labels = registry.registered(LifecycleEvent.PRE_TASK)
    assert labels == [f"script:{first}", f"script:{second}"]


def test_hooks_check_flags_missing_script(tmp_path: Path) -> None:
    config_file = tmp_path / "bernstein.yaml"
    config_file.write_text(
        "hooks:\n  pre_task:\n    - scripts/does-not-exist.sh\n",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(hooks_group, ["check", "--config", str(config_file)])
    assert result.exit_code == 1
    combined = result.output + (result.stderr if result.stderr_bytes is not None else "")
    assert "does-not-exist.sh" in combined


def test_hooks_check_flags_non_executable_script(tmp_path: Path) -> None:
    script = tmp_path / "notexec.sh"
    script.write_text("#!/usr/bin/env bash\ntrue\n", encoding="utf-8")
    # Strip any execute bits to simulate a missing chmod.
    script.chmod(script.stat().st_mode & ~(stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))

    config_file = tmp_path / "bernstein.yaml"
    config_file.write_text(
        f"hooks:\n  pre_task:\n    - {script}\n",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(hooks_group, ["check", "--config", str(config_file)])
    assert result.exit_code == 1
    combined = result.output + (result.stderr if result.stderr_bytes is not None else "")
    assert "not executable" in combined


def test_pluggy_bridge_dispatches_pre_task(tmp_path: Path) -> None:
    received: list[LifecycleContext] = []

    class MyPlugin:
        @hookimpl
        def pre_task(self, ctx: LifecycleContext) -> None:
            received.append(ctx)

    registry = HookRegistry()
    pm = apply_hooks_to_existing_system(registry)
    pm.register(MyPlugin())

    ctx = LifecycleContext(event=LifecycleEvent.PRE_TASK, task="T-77", workdir=tmp_path)
    registry.run(LifecycleEvent.PRE_TASK, ctx)

    assert len(received) == 1
    assert received[0].task == "T-77"


def test_ordering_preserved_between_scripts_and_callables(tmp_path: Path) -> None:
    order: list[str] = []
    marker_first = tmp_path / "first.log"
    marker_second = tmp_path / "second.log"
    first = _write_script(tmp_path / "first.sh", f"echo one > {marker_first}\n")
    second = _write_script(tmp_path / "second.sh", f"echo two > {marker_second}\n")

    def middle(ctx: LifecycleContext) -> None:
        order.append("callable")

    registry = HookRegistry()
    registry.register_script(LifecycleEvent.POST_SPAWN, first)
    registry.register_callable(LifecycleEvent.POST_SPAWN, middle)
    registry.register_script(LifecycleEvent.POST_SPAWN, second)

    # Confirm ordering ledger matches registration order before dispatch.
    labels = registry.registered(LifecycleEvent.POST_SPAWN)
    assert labels[0].startswith("script:")
    assert labels[1].startswith("callable:")
    assert labels[2].startswith("script:")

    ctx = LifecycleContext(event=LifecycleEvent.POST_SPAWN, workdir=tmp_path)
    registry.run(LifecycleEvent.POST_SPAWN, ctx)

    # first.sh must have produced its marker before second.sh - we rely on
    # mtimes as a coarse ordering check on filesystems that support them.
    assert marker_first.exists() and marker_second.exists()
    assert marker_first.stat().st_mtime <= marker_second.stat().st_mtime
    assert order == ["callable"]


def test_parent_env_is_not_leaked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A secret in the parent env must not reach the subprocess.
    monkeypatch.setenv("SUPER_SECRET_FOO", "leak-me")
    monkeypatch.setenv("BERNSTEIN_PROJECT", "proj")

    marker = tmp_path / "env.out"
    script = _write_script(
        tmp_path / "env.sh",
        f'{{ echo "SECRET=${{SUPER_SECRET_FOO:-unset}}"; echo "PROJECT=${{BERNSTEIN_PROJECT:-unset}}"; }} > {marker}\n',
    )

    registry = HookRegistry()
    registry.register_script(LifecycleEvent.PRE_SPAWN, script)
    ctx = LifecycleContext(event=LifecycleEvent.PRE_SPAWN, workdir=tmp_path)
    registry.run(LifecycleEvent.PRE_SPAWN, ctx)

    text = marker.read_text(encoding="utf-8")
    assert "SECRET=unset" in text
    assert "PROJECT=proj" in text
    # Cleanup: os.environ is scoped to this test via monkeypatch.
    assert os.environ.get("SUPER_SECRET_FOO") == "leak-me"


def test_dispatch_post_merge_event(tmp_path: Path) -> None:
    received: list[LifecycleContext] = []

    def handler(ctx: LifecycleContext) -> None:
        received.append(ctx)

    registry = HookRegistry()
    registry.register_callable(LifecycleEvent.POST_MERGE, handler)
    ctx = LifecycleContext(event=LifecycleEvent.POST_MERGE, task="T-42", workdir=tmp_path)
    registry.run(LifecycleEvent.POST_MERGE, ctx)

    assert len(received) == 1
    assert received[0].task == "T-42"
    assert received[0].event is LifecycleEvent.POST_MERGE
