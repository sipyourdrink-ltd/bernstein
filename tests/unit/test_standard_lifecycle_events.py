"""Unit tests for the standardised cross-CLI lifecycle events (T1323)."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.hooks_cmd import hooks as hooks_group
from bernstein.core.lifecycle.hooks import (
    DECISION_ALLOW,
    DECISION_ANNOTATE,
    DECISION_DENY,
    DECISION_MUTATE,
    STANDARD_EVENTS,
    HookDecision,
    HookDenied,
    HookRegistry,
    LifecycleContext,
    LifecycleEvent,
    discover_default_hook_scripts,
    parse_hook_decision,
)
from bernstein.core.lifecycle.payload_schemas import (
    PAYLOAD_SCHEMAS,
    PayloadSchemaError,
    validate_payload,
)
from bernstein.core.lifecycle.pluggy_bridge import (
    apply_hooks_to_existing_system,
)
from bernstein.core.persistence.workspace import grant_workspace_trust
from bernstein.plugins import hookimpl


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


def test_standard_event_names_exactly_match_issue_spec() -> None:
    """The 7 cross-CLI events must match the contract verbatim."""
    expected = [
        "sessionStart",
        "userPromptSubmitted",
        "preToolUse",
        "postToolUse",
        "errorOccurred",
        "idle",
        "sessionEnd",
    ]
    assert [event.value for event in STANDARD_EVENTS] == expected


@pytest.mark.parametrize("event", list(STANDARD_EVENTS))
def test_every_standard_event_has_payload_schema(event: LifecycleEvent) -> None:
    """Each new event must declare at least one required key."""
    assert event in PAYLOAD_SCHEMAS
    assert PAYLOAD_SCHEMAS[event].required, f"{event} should declare required keys"


def test_validate_payload_accepts_complete_data() -> None:
    validate_payload(
        LifecycleEvent.PRE_TOOL_USE,
        {"session_id": "s1", "tool": "shell.run", "args": {"command": "ls"}},
    )


def test_validate_payload_rejects_missing_key() -> None:
    with pytest.raises(PayloadSchemaError) as excinfo:
        validate_payload(
            LifecycleEvent.PRE_TOOL_USE,
            {"session_id": "s1", "tool": "shell.run"},
        )
    assert "args" in str(excinfo.value)


def test_validate_payload_ignores_extra_keys() -> None:
    """Extra keys are forward-compatible - schemas are additive."""
    validate_payload(
        LifecycleEvent.IDLE,
        {"session_id": "s1", "idle_duration_s": 5, "extra": "ok"},
    )


def test_legacy_events_have_no_schema_so_anything_validates() -> None:
    """Pre-existing snake_case events accept arbitrary payloads."""
    validate_payload(LifecycleEvent.PRE_TASK, {"anything": "goes"})


def test_parse_hook_decision_handles_empty_and_garbage() -> None:
    assert parse_hook_decision(b"") is None
    assert parse_hook_decision(b"not json") is None
    assert parse_hook_decision(b"[1, 2, 3]") is None  # array, not object


def test_parse_hook_decision_normalises_unknown_verb() -> None:
    decision = parse_hook_decision(b'{"decision": "explode", "reason": "x"}')
    assert decision is not None
    assert decision.decision == DECISION_ALLOW
    assert decision.raw == {"decision": "explode", "reason": "x"}


def test_parse_hook_decision_extracts_full_record() -> None:
    decision = parse_hook_decision(
        b'{"decision": "annotate", "data": {"k": 1}, "extra": "kept"}',
    )
    assert decision == HookDecision(
        decision=DECISION_ANNOTATE,
        reason="",
        data={"k": 1},
        raw={"decision": "annotate", "data": {"k": 1}, "extra": "kept"},
    )


def test_pre_tool_use_deny_blocks_with_hookdenied(tmp_path: Path) -> None:
    """A preToolUse hook returning ``deny`` raises HookDenied."""
    script = _write_script(
        tmp_path / "deny.sh",
        'echo \'{"decision": "deny", "reason": "forbidden"}\'\n',
    )
    registry = HookRegistry()
    registry.register_script(LifecycleEvent.PRE_TOOL_USE, script)
    ctx = LifecycleContext(
        event=LifecycleEvent.PRE_TOOL_USE,
        session_id="s1",
        workdir=tmp_path,
        data={"session_id": "s1", "tool": "shell.run", "args": {}},
    )
    with pytest.raises(HookDenied) as excinfo:
        registry.run(LifecycleEvent.PRE_TOOL_USE, ctx)
    assert excinfo.value.reason == "forbidden"
    assert excinfo.value.event is LifecycleEvent.PRE_TOOL_USE


def test_annotate_decision_merges_into_context(tmp_path: Path) -> None:
    script = _write_script(
        tmp_path / "annotate.sh",
        'echo \'{"decision": "annotate", "data": {"added": "yes"}}\'\n',
    )
    registry = HookRegistry()
    registry.register_script(LifecycleEvent.POST_TOOL_USE, script)
    ctx = LifecycleContext(
        event=LifecycleEvent.POST_TOOL_USE,
        session_id="s1",
        workdir=tmp_path,
        data={"session_id": "s1", "tool": "t", "args": {}, "result": ""},
    )
    result = registry.run(LifecycleEvent.POST_TOOL_USE, ctx)
    assert result.data["added"] == "yes"
    assert result.data["session_id"] == "s1"


def test_mutate_decision_replaces_data(tmp_path: Path) -> None:
    script = _write_script(
        tmp_path / "mutate.sh",
        'echo \'{"decision": "mutate", "data": {"only": "new"}}\'\n',
    )
    registry = HookRegistry()
    registry.register_script(LifecycleEvent.PRE_TOOL_USE, script)
    ctx = LifecycleContext(
        event=LifecycleEvent.PRE_TOOL_USE,
        workdir=tmp_path,
        data={"old": "gone"},
    )
    result = registry.run(LifecycleEvent.PRE_TOOL_USE, ctx)
    assert result.data == {"only": "new"}


def test_allow_decision_is_a_noop(tmp_path: Path) -> None:
    """Default `allow` (or missing stdout) leaves context untouched."""
    script = _write_script(tmp_path / "allow.sh", "true\n")
    registry = HookRegistry()
    registry.register_script(LifecycleEvent.SESSION_START, script)
    ctx = LifecycleContext(
        event=LifecycleEvent.SESSION_START,
        workdir=tmp_path,
        data={"session_id": "s1"},
    )
    result = registry.run(LifecycleEvent.SESSION_START, ctx)
    assert result.data == {"session_id": "s1"}


def test_discover_default_hook_scripts(tmp_path: Path) -> None:
    hook_dir = tmp_path / ".bernstein" / "hooks"
    hook_dir.mkdir(parents=True)
    pre = _write_script(hook_dir / "preToolUse.sh", "true\n")
    post = _write_script(hook_dir / "postToolUse.sh", "true\n")
    py_hook = hook_dir / "sessionStart.py"
    py_hook.write_text("# noop\n", encoding="utf-8")
    py_hook.chmod(py_hook.stat().st_mode | stat.S_IXUSR)
    # Files that should be ignored.
    (hook_dir / "README.md").write_text("docs\n", encoding="utf-8")
    junk = _write_script(hook_dir / "not_an_event.sh", "true\n")

    discovered = discover_default_hook_scripts(tmp_path)
    assert discovered[LifecycleEvent.PRE_TOOL_USE] == [pre]
    assert discovered[LifecycleEvent.POST_TOOL_USE] == [post]
    assert discovered[LifecycleEvent.SESSION_START] == [py_hook]
    # Unrelated file did not slip into any bucket.
    for paths in discovered.values():
        assert junk not in paths


def test_discover_returns_empty_when_dir_missing(tmp_path: Path) -> None:
    discovered = discover_default_hook_scripts(tmp_path)
    assert all(scripts == [] for scripts in discovered.values())


def test_pluggy_bridge_dispatches_session_start(tmp_path: Path) -> None:
    received: list[LifecycleContext] = []

    class Plugin:
        @hookimpl
        def sessionStart(self, ctx: LifecycleContext) -> None:
            received.append(ctx)

    registry = HookRegistry()
    pm = apply_hooks_to_existing_system(registry)
    pm.register(Plugin())

    ctx = LifecycleContext(
        event=LifecycleEvent.SESSION_START,
        session_id="s9",
        workdir=tmp_path,
        data={"session_id": "s9"},
    )
    registry.run(LifecycleEvent.SESSION_START, ctx)
    assert len(received) == 1
    assert received[0].session_id == "s9"


def test_pluggy_bridge_dispatches_pre_tool_use(tmp_path: Path) -> None:
    received: list[LifecycleContext] = []

    class Plugin:
        @hookimpl
        def preToolUse(self, ctx: LifecycleContext) -> None:
            received.append(ctx)

    registry = HookRegistry()
    pm = apply_hooks_to_existing_system(registry)
    pm.register(Plugin())

    ctx = LifecycleContext(
        event=LifecycleEvent.PRE_TOOL_USE,
        session_id="s1",
        workdir=tmp_path,
        data={"session_id": "s1", "tool": "shell.run", "args": {}},
    )
    registry.run(LifecycleEvent.PRE_TOOL_USE, ctx)
    assert len(received) == 1


def test_hooks_dry_run_cli_smoke_test(tmp_path: Path) -> None:
    """`bernstein hooks dry-run sessionStart` fires the sample payload."""
    config_file = tmp_path / "bernstein.yaml"
    config_file.write_text("hooks: {}\n", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        hooks_group,
        ["dry-run", "sessionStart", "--config", str(config_file)],
    )
    assert result.exit_code == 0, result.output
    assert "OK: sessionStart" in result.output
    # The serialized context should include the sample payload data.
    assert "dryrun-session" in result.output


def test_hooks_dry_run_invokes_default_hook(tmp_path: Path) -> None:
    """A `.bernstein/hooks/preToolUse.sh` is auto-registered + executed."""
    config_file = tmp_path / "bernstein.yaml"
    config_file.write_text("hooks: {}\n", encoding="utf-8")
    hook_dir = tmp_path / ".bernstein" / "hooks"
    hook_dir.mkdir(parents=True)
    marker = tmp_path / "ran.txt"
    _write_script(
        hook_dir / "preToolUse.sh",
        f'echo ran > {marker}\necho \'{{"decision": "allow"}}\'\n',
    )

    runner = CliRunner()
    result = runner.invoke(
        hooks_group,
        ["dry-run", "preToolUse", "--config", str(config_file)],
    )
    assert result.exit_code == 0, result.output
    assert marker.exists()


def test_hooks_dry_run_blocks_on_deny(tmp_path: Path) -> None:
    config_file = tmp_path / "bernstein.yaml"
    config_file.write_text("hooks: {}\n", encoding="utf-8")
    hook_dir = tmp_path / ".bernstein" / "hooks"
    hook_dir.mkdir(parents=True)
    _write_script(
        hook_dir / "preToolUse.sh",
        'echo \'{"decision": "deny", "reason": "policy violation"}\'\n',
    )

    runner = CliRunner()
    result = runner.invoke(
        hooks_group,
        ["dry-run", "preToolUse", "--config", str(config_file)],
    )
    assert result.exit_code == 2
    combined = result.output + (result.stderr if result.stderr_bytes is not None else "")
    assert "DENIED" in combined
    assert "policy violation" in combined


def test_hooks_dry_run_validates_explicit_payload(tmp_path: Path) -> None:
    """`--payload` is schema-validated up-front."""
    config_file = tmp_path / "bernstein.yaml"
    config_file.write_text("hooks: {}\n", encoding="utf-8")
    payload = tmp_path / "bad.json"
    payload.write_text(json.dumps({"session_id": "s1"}), encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        hooks_group,
        [
            "dry-run",
            "preToolUse",
            "--payload",
            str(payload),
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 1
    combined = result.output + (result.stderr if result.stderr_bytes is not None else "")
    assert "missing required keys" in combined


def test_hooks_dry_run_accepts_custom_payload(tmp_path: Path) -> None:
    config_file = tmp_path / "bernstein.yaml"
    config_file.write_text("hooks: {}\n", encoding="utf-8")
    payload = tmp_path / "good.json"
    payload.write_text(
        json.dumps({"session_id": "abc", "idle_duration_s": 42}),
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(
        hooks_group,
        [
            "dry-run",
            "idle",
            "--payload",
            str(payload),
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "OK: idle" in result.output
    assert '"idle_duration_s": 42' in result.output


def test_hooks_list_includes_standard_events(tmp_path: Path) -> None:
    """`hooks list` enumerates the new event family alongside legacy ones."""
    config_file = tmp_path / "bernstein.yaml"
    config_file.write_text("hooks: {}\n", encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(hooks_group, ["list", "--config", str(config_file)])
    assert result.exit_code == 0
    for event in STANDARD_EVENTS:
        assert event.value in result.output


def test_legacy_events_still_function(tmp_path: Path) -> None:
    """Pre-existing snake_case events remain fully usable."""
    marker = tmp_path / "out.txt"
    script = _write_script(tmp_path / "h.sh", f"echo hi > {marker}\n")
    registry = HookRegistry()
    registry.register_script(LifecycleEvent.PRE_TASK, script)
    ctx = LifecycleContext(event=LifecycleEvent.PRE_TASK, workdir=tmp_path)
    registry.run(LifecycleEvent.PRE_TASK, ctx)
    assert marker.exists()


def test_decision_constants_are_distinct() -> None:
    assert {DECISION_ALLOW, DECISION_DENY, DECISION_MUTATE, DECISION_ANNOTATE} == {
        "allow",
        "deny",
        "mutate",
        "annotate",
    }


def test_hookfailure_does_not_obscure_hookdenied(tmp_path: Path) -> None:
    """HookDenied propagates rather than being wrapped as HookFailure."""
    script = _write_script(
        tmp_path / "deny.sh",
        'echo \'{"decision": "deny", "reason": "no"}\'\n',
    )
    registry = HookRegistry()
    registry.register_script(LifecycleEvent.PRE_TOOL_USE, script)
    ctx = LifecycleContext(
        event=LifecycleEvent.PRE_TOOL_USE,
        workdir=tmp_path,
        data={"session_id": "s", "tool": "t", "args": {}},
    )
    with pytest.raises(HookDenied):
        registry.run(LifecycleEvent.PRE_TOOL_USE, ctx)
    # HookFailure must not have been raised in place of HookDenied.
    # The check above is sufficient since pytest only matches HookDenied.


def test_callable_in_chain_receives_mutated_context(tmp_path: Path) -> None:
    """A callable registered after a mutating script sees the new data."""
    script = _write_script(
        tmp_path / "mutate.sh",
        'echo \'{"decision": "annotate", "data": {"flag": true}}\'\n',
    )
    seen: list[LifecycleContext] = []

    def watcher(ctx: LifecycleContext) -> None:
        seen.append(ctx)

    registry = HookRegistry()
    registry.register_script(LifecycleEvent.SESSION_START, script)
    registry.register_callable(LifecycleEvent.SESSION_START, watcher)
    ctx = LifecycleContext(
        event=LifecycleEvent.SESSION_START,
        workdir=tmp_path,
        data={"session_id": "s1"},
    )
    registry.run(LifecycleEvent.SESSION_START, ctx)
    assert seen and seen[0].data.get("flag") is True
