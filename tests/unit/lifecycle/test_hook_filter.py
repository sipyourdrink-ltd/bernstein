"""Unit tests for the permission-rule prefilter on lifecycle hooks.

Covers the hook ``if:`` filter grammar parser (match, non-match, parse
error, legacy no-filter), plus the registry-level deny-fast behaviour:
a non-matching filter skips the subprocess spawn and emits a
``hook.filtered`` metric, while a matching filter runs the script.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from bernstein.core.config.hook_config import HookConfigError, apply_config, parse_hook_config
from bernstein.core.lifecycle.hook_filter import (
    HookFilter,
    HookFilterError,
    parse_hook_filter,
)
from bernstein.core.lifecycle.hooks import (
    HookRegistry,
    LifecycleContext,
    LifecycleEvent,
)
from bernstein.core.persistence.workspace import grant_workspace_trust

# --------------------------------------------------------------------------- helpers


@pytest.fixture(autouse=True)
def _trusted_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Script hooks only run in a trusted workspace; trust each test's tmp dir."""
    grant_workspace_trust(tmp_path)
    monkeypatch.chdir(tmp_path)


def _write_script(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


# --------------------------------------------------------------------------- parsing


def test_parse_none_returns_no_filter() -> None:
    """The legacy no-filter case: absent ``if:`` always matches."""
    assert parse_hook_filter(None) is None


def test_parse_bash_command_filter() -> None:
    f = parse_hook_filter("Bash(git *)")
    assert isinstance(f, HookFilter)
    assert f.rule.tool == "Bash"
    assert f.rule.command == "git *"
    assert f.rule.path is None


def test_parse_read_path_filter() -> None:
    f = parse_hook_filter("Read(/etc/*)")
    assert f is not None
    assert f.rule.tool == "Read"
    assert f.rule.path == "/etc/*"
    assert f.rule.command is None


def test_parse_tool_keyword_filter() -> None:
    f = parse_hook_filter("Tool(grep)")
    assert f is not None
    assert f.rule.tool == "grep"
    assert f.rule.command is None
    assert f.rule.path is None


def test_parse_bare_tool_filter() -> None:
    f = parse_hook_filter("Bash")
    assert f is not None
    assert f.rule.tool == "Bash"
    assert f.rule.command is None
    assert f.rule.path is None


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "Bash(",
        "Bash)",
        "Bash()",
        "Tool",
        "Tool()",
        "Bash(git *) extra",
        "123(x)",
    ],
)
def test_parse_error_surfaces_eagerly(bad: str) -> None:
    """Malformed filters raise at parse time, not at dispatch time."""
    with pytest.raises(HookFilterError):
        parse_hook_filter(bad)


# --------------------------------------------------------------------------- matching


def test_command_filter_matches_payload() -> None:
    f = parse_hook_filter("Bash(git *)")
    assert f is not None
    assert f.matches({"tool": "Bash", "args": {"command": "git push"}}) is True


def test_command_filter_non_match() -> None:
    f = parse_hook_filter("Bash(git *)")
    assert f is not None
    assert f.matches({"tool": "Bash", "args": {"command": "rm -rf /"}}) is False


def test_path_filter_matches_and_non_match() -> None:
    f = parse_hook_filter("Read(/etc/**)")
    assert f is not None
    assert f.matches({"tool": "Read", "args": {"path": "/etc/passwd"}}) is True
    assert f.matches({"tool": "Read", "args": {"file_path": "/home/x"}}) is False


def test_tool_name_mismatch_does_not_match() -> None:
    f = parse_hook_filter("Bash(git *)")
    assert f is not None
    assert f.matches({"tool": "Write", "args": {"command": "git push"}}) is False


def test_payload_without_tool_never_matches() -> None:
    f = parse_hook_filter("Bash")
    assert f is not None
    assert f.matches({}) is False
    assert f.matches({"session_id": "S-1"}) is False


def test_tool_name_match_is_case_insensitive() -> None:
    f = parse_hook_filter("Bash")
    assert f is not None
    assert f.matches({"tool": "bash"}) is True


# --------------------------------------------------------------------------- registry: allow path


def test_matching_filter_runs_script(tmp_path: Path) -> None:
    """Allow path: a matching filter spawns the hook subprocess."""
    marker = tmp_path / "ran.txt"
    script = _write_script(tmp_path / "hook.sh", f"echo ran > {marker}\n")

    registry = HookRegistry()
    registry.register_script(
        LifecycleEvent.PRE_TOOL_USE,
        script,
        hook_filter=parse_hook_filter("Bash(git *)"),
    )
    ctx = LifecycleContext(
        event=LifecycleEvent.PRE_TOOL_USE,
        workdir=tmp_path,
        data={"tool": "Bash", "args": {"command": "git status"}},
    )
    registry.run(LifecycleEvent.PRE_TOOL_USE, ctx)

    assert marker.exists()


def test_no_filter_always_runs(tmp_path: Path) -> None:
    """Legacy no-filter case: the hook runs regardless of payload."""
    marker = tmp_path / "ran.txt"
    script = _write_script(tmp_path / "hook.sh", f"echo ran > {marker}\n")

    registry = HookRegistry()
    registry.register_script(LifecycleEvent.PRE_TOOL_USE, script)
    ctx = LifecycleContext(
        event=LifecycleEvent.PRE_TOOL_USE,
        workdir=tmp_path,
        data={"tool": "Write", "args": {"path": "/x"}},
    )
    registry.run(LifecycleEvent.PRE_TOOL_USE, ctx)

    assert marker.exists()


# --------------------------------------------------------------------------- registry: deny-fast path


def test_non_matching_filter_skips_spawn_and_emits_metric(tmp_path: Path) -> None:
    """Deny-fast path: a non-matching filter never spawns the subprocess."""
    marker = tmp_path / "ran.txt"
    script = _write_script(tmp_path / "hook.sh", f"echo ran > {marker}\n")

    emitted: list[tuple[str, str, str]] = []

    registry = HookRegistry()
    registry.bind_filtered_metric_sink(
        lambda event, hook, reason: emitted.append((event.value, hook, reason)),
    )
    registry.register_script(
        LifecycleEvent.PRE_TOOL_USE,
        script,
        hook_filter=parse_hook_filter("Bash(git *)"),
    )
    ctx = LifecycleContext(
        event=LifecycleEvent.PRE_TOOL_USE,
        workdir=tmp_path,
        data={"tool": "Bash", "args": {"command": "rm -rf /"}},
    )
    registry.run(LifecycleEvent.PRE_TOOL_USE, ctx)

    assert not marker.exists()  # subprocess never ran
    assert len(emitted) == 1
    event_value, hook_label, reason = emitted[0]
    assert event_value == LifecycleEvent.PRE_TOOL_USE.value
    assert str(script) in hook_label
    assert "Bash(git *)" in reason


def test_metric_sink_failure_is_swallowed(tmp_path: Path) -> None:
    """A broken metric sink must not break the dispatch loop."""
    script = _write_script(tmp_path / "hook.sh", "echo ran\n")

    def _boom(*_args: object) -> None:
        raise RuntimeError("sink down")

    registry = HookRegistry()
    registry.bind_filtered_metric_sink(_boom)
    registry.register_script(
        LifecycleEvent.PRE_TOOL_USE,
        script,
        hook_filter=parse_hook_filter("Bash(git *)"),
    )
    ctx = LifecycleContext(
        event=LifecycleEvent.PRE_TOOL_USE,
        workdir=tmp_path,
        data={"tool": "Write", "args": {}},
    )
    # Should not raise even though the sink raises.
    registry.run(LifecycleEvent.PRE_TOOL_USE, ctx)


# --------------------------------------------------------------------------- rule precedence


def test_rule_precedence_first_matching_hook_runs(tmp_path: Path) -> None:
    """Hooks fire in registration order; only filters that match run.

    Two hooks are registered for the same event with disjoint filters. For
    a given payload exactly one runs, and a third no-filter hook always
    runs, preserving declaration order among the survivors.
    """
    ran: list[str] = []
    script_a = _write_script(tmp_path / "a.sh", f"echo a >> {tmp_path / 'log'}\n")
    script_b = _write_script(tmp_path / "b.sh", f"echo b >> {tmp_path / 'log'}\n")
    script_c = _write_script(tmp_path / "c.sh", f"echo c >> {tmp_path / 'log'}\n")

    registry = HookRegistry()
    registry.register_script(
        LifecycleEvent.PRE_TOOL_USE,
        script_a,
        hook_filter=parse_hook_filter("Bash(git *)"),
    )
    registry.register_script(
        LifecycleEvent.PRE_TOOL_USE,
        script_b,
        hook_filter=parse_hook_filter("Write(/secret/*)"),
    )
    registry.register_script(LifecycleEvent.PRE_TOOL_USE, script_c)

    ctx = LifecycleContext(
        event=LifecycleEvent.PRE_TOOL_USE,
        workdir=tmp_path,
        data={"tool": "Bash", "args": {"command": "git commit"}},
    )
    registry.run(LifecycleEvent.PRE_TOOL_USE, ctx)

    ran = (tmp_path / "log").read_text(encoding="utf-8").split()
    # script_a matches the Bash payload, script_b does not, script_c always runs.
    assert ran == ["a", "c"]


# --------------------------------------------------------------------------- config integration


def test_config_parses_if_filter(tmp_path: Path) -> None:
    raw = {
        "preToolUse": [
            {"script": "scripts/guard.sh", "if": "Bash(git push *)"},
        ],
    }
    config = parse_hook_config(raw)
    entries = config.scripts[LifecycleEvent.PRE_TOOL_USE]
    assert len(entries) == 1
    assert entries[0].hook_filter is not None
    assert entries[0].hook_filter.rule.command == "git push *"


def test_config_malformed_filter_raises_at_load() -> None:
    """A malformed ``if:`` prevents registration with a clear config error."""
    raw = {"preToolUse": [{"script": "scripts/guard.sh", "if": "Bash("}]}
    with pytest.raises(HookConfigError) as excinfo:
        parse_hook_config(raw)
    assert "if is invalid" in str(excinfo.value)


def test_config_non_string_filter_raises() -> None:
    raw = {"preToolUse": [{"script": "scripts/guard.sh", "if": 42}]}
    with pytest.raises(HookConfigError):
        parse_hook_config(raw)


def test_apply_config_propagates_filter(tmp_path: Path) -> None:
    """apply_config carries the parsed filter through to the registry."""
    marker = tmp_path / "ran.txt"
    script = _write_script(tmp_path / "guard.sh", f"echo ran > {marker}\n")
    raw = {"preToolUse": [{"script": str(script), "if": "Bash(git *)"}]}
    config = parse_hook_config(raw)

    registry = HookRegistry()
    apply_config(registry, config)

    # Non-matching payload -> skipped.
    registry.run(
        LifecycleEvent.PRE_TOOL_USE,
        LifecycleContext(
            event=LifecycleEvent.PRE_TOOL_USE,
            workdir=tmp_path,
            data={"tool": "Bash", "args": {"command": "ls"}},
        ),
    )
    assert not marker.exists()

    # Matching payload -> runs.
    registry.run(
        LifecycleEvent.PRE_TOOL_USE,
        LifecycleContext(
            event=LifecycleEvent.PRE_TOOL_USE,
            workdir=tmp_path,
            data={"tool": "Bash", "args": {"command": "git log"}},
        ),
    )
    assert marker.exists()
