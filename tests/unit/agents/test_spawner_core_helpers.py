"""Behavioral tests for ``spawner_core`` module-level helpers + accessors.

Targets the deterministic prompt-shaping and config helpers that are not
exercised by the heavier spawn-path tests: log sanitisation, the
auth-section absolute-path rendering, the health-check cron interval
ladder, the ``/batch`` prompt builder, scheduled-task injection, and the
graceful-empty paths of persistent-memory / RAG context. Also covers the
simple ``AgentSpawner`` accessors/mutators (worktree path lookup,
shutdown-event wiring, merge-queue wiring, sandbox session) which only
require a worktree-disabled spawner with a mock adapter.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from bernstein.adapters.base import CLIAdapter
from bernstein.core.agents.spawner_core import (
    AgentSpawner,
    _build_rag_context,
    _health_check_interval,
    _inject_scheduled_tasks,
    _load_persistent_memory,
    _render_auth_section,
    _render_batch_prompt,
    _render_signal_check,
    _sanitise_for_log,
)


def _task(make_task: Any, **overrides: Any) -> Any:
    return make_task(**overrides)


# ---------------------------------------------------------------------------
# _sanitise_for_log
# ---------------------------------------------------------------------------


def test_sanitise_for_log_strips_crlf() -> None:
    """CR and LF are removed so log lines cannot be forged."""
    assert _sanitise_for_log("line1\nline2\rline3") == "line1line2line3"


def test_sanitise_for_log_empty_passthrough() -> None:
    """An empty string is returned unchanged (cheap fast path)."""
    assert _sanitise_for_log("") == ""


def test_sanitise_for_log_clean_passthrough() -> None:
    """A string with no control chars is unchanged."""
    assert _sanitise_for_log("normal session id") == "normal session id"


# ---------------------------------------------------------------------------
# _render_auth_section
# ---------------------------------------------------------------------------


def test_render_auth_section_uses_absolute_path() -> None:
    """An already-absolute token path is embedded verbatim."""
    out = _render_auth_section(Path("/tmp/tok.jwt"))
    assert "/tmp/tok.jwt" in out
    assert "Authorization: Bearer" in out


def test_render_auth_section_resolves_relative_path() -> None:
    """A relative token path is resolved to absolute before embedding."""
    rel = Path("rel/tok.jwt")
    out = _render_auth_section(rel)
    assert str(rel.resolve()) in out


def test_render_auth_section_documents_env_fallback() -> None:
    """The section documents the BERNSTEIN_AUTH_TOKEN env fallback."""
    assert "BERNSTEIN_AUTH_TOKEN" in _render_auth_section(Path("/tmp/tok.jwt"))


def test_render_auth_section_does_not_embed_token_value() -> None:
    """Only the path is embedded; the section instructs to cat the file."""
    out = _render_auth_section(Path("/tmp/tok.jwt"))
    assert "$(cat /tmp/tok.jwt)" in out


def test_render_auth_section_mirrors_manager_template_shell_contract() -> None:
    """Mirrors the e2da807d manager-prompt auth contract (see templates/roles/manager).

    The rendered section must:
    - mandate run_command's single-STRING shell form for the auth curls,
    - warn that the argv-list form silently drops ``$(...)``/``$VAR`` expansion,
    - require checking the trailing HTTP status via ``-w '\\n%{http_code}'``
      and retrying on non-2xx,
    - contain the string-form curl with ``$(cat <token path>)``.
    """
    out = _render_auth_section(Path("/tmp/tok.jwt"))
    # Single-STRING form mandate + argv-list expansion warning.
    assert "single command **STRING**" in out
    assert "argv LIST" in out
    assert "never expanded" in out
    assert "single-STRING" in out
    # String-form curl example interpolating the token path.
    assert '-H "Authorization: Bearer $(cat /tmp/tok.jwt)"' in out
    assert "ONE string" in out
    # HTTP status check + retry-on-non-2xx instruction.
    assert "-w '\\n%{http_code}'" in out
    assert "200-299" in out
    assert "retry" in out
    # Example curls carry the status-code write-out.
    assert "curl -sS -w '\\n%{http_code}'" in out


def test_render_auth_section_has_no_read_file_token_fallback() -> None:
    """The unreachable read_file-token fallback is gone; read_file is prohibited.

    e2da807d removed the read_file fallback from the manager templates because
    read_file is confined to the agent worktree and the token lives outside it.
    The rendered section must not instruct agents to read the token via
    read_file - the only read_file mention allowed is the explicit prohibition.
    """
    out = _render_auth_section(Path("/tmp/tok.jwt"))
    assert "**Do not use the `read_file` tool to obtain your token.**" in out
    # Every read_file mention must be part of the prohibition paragraph,
    # never a fallback instruction.
    prohibition_start = out.index("**Do not use the `read_file` tool")
    prohibition_end = out.index("\n\n", prohibition_start)
    outside = out[:prohibition_start] + out[prohibition_end:]
    assert "read_file" not in outside
    # No fallback phrasing anywhere.
    assert "fall back to `read_file`" not in out
    assert "use the `read_file` tool to read" not in out


# ---------------------------------------------------------------------------
# _render_signal_check
# ---------------------------------------------------------------------------


def test_render_signal_check_embeds_session_paths() -> None:
    """The signal-check block references WAKEUP and SHUTDOWN by session."""
    block = _render_signal_check("SESS-3")
    assert ".sdd/runtime/signals/SESS-3/WAKEUP" in block
    assert ".sdd/runtime/signals/SESS-3/SHUTDOWN" in block


# ---------------------------------------------------------------------------
# _health_check_interval
# ---------------------------------------------------------------------------


def test_health_check_interval_empty_default() -> None:
    """An empty task list defaults to a 5-minute interval."""
    assert _health_check_interval([]) == 5


def test_health_check_interval_short_tasks(make_task: Any) -> None:
    """A short estimate (<15 min) polls every 3 minutes."""
    task = make_task()
    task.estimated_minutes = 10
    assert _health_check_interval([task]) == 3


def test_health_check_interval_long_tasks(make_task: Any) -> None:
    """A long estimate (>60 min) polls every 10 minutes."""
    task = make_task()
    task.estimated_minutes = 90
    assert _health_check_interval([task]) == 10


def test_health_check_interval_medium_tasks(make_task: Any) -> None:
    """A medium estimate polls every 5 minutes."""
    task = make_task()
    task.estimated_minutes = 30
    assert _health_check_interval([task]) == 5


def test_health_check_interval_uses_max_estimate(make_task: Any) -> None:
    """The interval keys off the longest task in the batch."""
    short = make_task(id="A")
    short.estimated_minutes = 5
    long = make_task(id="B")
    long.estimated_minutes = 120
    # max estimate 120 > 60 -> 10 minute interval despite the short sibling.
    assert _health_check_interval([short, long]) == 10


# ---------------------------------------------------------------------------
# _render_batch_prompt
# ---------------------------------------------------------------------------


def test_render_batch_prompt_starts_with_batch_command(make_task: Any) -> None:
    """The batch prompt opens with /batch + the task description."""
    task = make_task(description="rename all foo to bar")
    prompt = _render_batch_prompt(task)
    assert prompt.startswith("/batch rename all foo to bar")


def test_render_batch_prompt_lists_affected_paths(make_task: Any) -> None:
    """Owned files are surfaced as affected paths."""
    task = make_task(owned_files=["a.py", "b.py"])
    prompt = _render_batch_prompt(task)
    assert "Affected paths: a.py, b.py" in prompt


def test_render_batch_prompt_omits_paths_when_none(make_task: Any) -> None:
    """With no owned files, no affected-paths line appears."""
    task = make_task(owned_files=[])
    assert "Affected paths" not in _render_batch_prompt(task)


def test_render_batch_prompt_includes_completion_curl(make_task: Any) -> None:
    """The prompt embeds the per-task completion endpoint."""
    task = make_task(id="T-77")
    assert "/tasks/T-77/complete" in _render_batch_prompt(task)


# ---------------------------------------------------------------------------
# _inject_scheduled_tasks
# ---------------------------------------------------------------------------


def test_inject_scheduled_tasks_writes_cron_payload(tmp_path: Path) -> None:
    """A recurring health-check cron task is written to .claude/scheduled_tasks.json."""
    _inject_scheduled_tasks(tmp_path, "abcdef123456789", health_interval_minutes=7)
    payload = json.loads((tmp_path / ".claude" / "scheduled_tasks.json").read_text(encoding="utf-8"))
    task = payload["tasks"][0]
    assert task["cron"] == "*/7 * * * *"
    assert task["recurring"] is True
    # The task id derives from the first 8 chars of the session id: "abcdef12".
    assert task["id"] == "hc-abcdef12"


def test_inject_scheduled_tasks_default_interval(tmp_path: Path) -> None:
    """The default interval is 5 minutes."""
    _inject_scheduled_tasks(tmp_path, "S1")
    payload = json.loads((tmp_path / ".claude" / "scheduled_tasks.json").read_text(encoding="utf-8"))
    assert payload["tasks"][0]["cron"] == "*/5 * * * *"


# ---------------------------------------------------------------------------
# _load_persistent_memory / _build_rag_context - graceful empty paths
# ---------------------------------------------------------------------------


def test_load_persistent_memory_missing_db_returns_empty(tmp_path: Path) -> None:
    """No memory.db means an empty memory section (no crash)."""
    assert _load_persistent_memory(tmp_path, ["backend"]) == ""


def test_build_rag_context_empty_index_returns_empty(tmp_path: Path, make_task: Any) -> None:
    """An empty/unindexed workdir yields no RAG context."""
    assert _build_rag_context([make_task()], tmp_path, None) == ""


# ---------------------------------------------------------------------------
# AgentSpawner simple accessors / mutators
# ---------------------------------------------------------------------------


def _spawner(tmp_path: Path) -> AgentSpawner:
    adapter = MagicMock(spec=CLIAdapter)
    return AgentSpawner(adapter, tmp_path, tmp_path, use_worktrees=False)


def test_get_worktree_path_unknown_session_is_none(tmp_path: Path) -> None:
    """An unregistered session has no worktree path."""
    assert _spawner(tmp_path).get_worktree_path("never-spawned") is None


def test_set_shutdown_event_stores_event(tmp_path: Path) -> None:
    """The shutdown event is stored on the spawner."""
    spawner = _spawner(tmp_path)
    event = threading.Event()
    spawner.set_shutdown_event(event)
    assert spawner._shutdown_event is event


def test_spawn_refused_during_shutdown_is_logged(tmp_path: Path, make_task: Any, caplog: Any) -> None:
    """Logging gap: a spawn attempt during shutdown raised
    ``ShutdownInProgress`` with no log line -- an operator reading only the
    server-side log (not the orchestrator's exception traceback) had no way
    to see that a spawn was refused and why. Assert the refusal logs the
    role, task count, and task ids being refused."""
    import logging

    from bernstein.core.orchestration.orchestrator import ShutdownInProgress

    caplog.set_level(logging.INFO, logger="bernstein.core.agents.spawner_core")
    spawner = _spawner(tmp_path)
    event = threading.Event()
    event.set()
    spawner.set_shutdown_event(event)

    task = make_task(id="task-shutdown-1")
    try:
        spawner.spawn_for_tasks([task])
        raise AssertionError("expected ShutdownInProgress")
    except ShutdownInProgress:
        pass

    messages = [r.message for r in caplog.records]
    assert any("spawn refused" in m and "shutdown_event is set" in m and "task-shutdown-1" in m for m in messages), (
        messages
    )


def test_set_merge_queue_stores_queue(tmp_path: Path) -> None:
    """The merge queue is stored for FIFO merges."""
    spawner = _spawner(tmp_path)
    queue = MagicMock()
    spawner.set_merge_queue(queue)
    assert spawner._merge_queue is queue


def test_sandbox_session_defaults_none(tmp_path: Path) -> None:
    """A spawner with no sandbox backend exposes None."""
    assert _spawner(tmp_path).sandbox_session is None


def test_cleanup_worktree_unknown_session_is_safe(tmp_path: Path) -> None:
    """Cleaning up an unknown session does not raise."""
    _spawner(tmp_path).cleanup_worktree("unknown-session")
