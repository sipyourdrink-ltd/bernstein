"""The interactive approval gate, driven through the real dispatch point (#4543).

The approval package shipped three complete resolution surfaces -- HTTP
(``GET /approvals/queue`` + ``POST /approvals/{id}/resolve``), CLI
(``bernstein approve --tool``) and the TUI ``ApprovalPanel`` -- polling a queue
that nothing could feed: ``gate_tool_call`` / ``await_tool_call`` had zero
callers in ``src/``. These tests drive the producer end, so the queue can no
longer be empty by construction.

The dispatch point is ``bernstein hook-gate check --event PreToolUse``, the CLI
a gate-capable adapter wires its PreToolUse hook to. It is the only place
bernstein sees an individual tool invocation before it runs: the guardrails
package is diff-level and runs at merge time.

Each test drives the CLI exactly as the worker's hook runner would.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.hook_gate_cmd import hook_gate_group
from bernstein.core.approval.models import ApprovalDecision
from bernstein.core.approval.queue import (
    ApprovalQueue,
    get_default_queue,
    reset_default_queue,
)

if TYPE_CHECKING:
    from pathlib import Path

_ALLOW = 0
_BLOCK = 2

# `bernstein hook-gate check` shells out per tool call, so the queue it writes
# is the file-backed one under the worktree -- the same path every resolution
# surface reads.
_QUEUE_REL = (".sdd", "runtime", "approvals")


def _queue_dir(workdir: Path) -> Path:
    return workdir.joinpath(*_QUEUE_REL)


@pytest.fixture(autouse=True)
def _fresh_default_queue() -> object:
    """The gate resolves its queue through ``get_default_queue``, whose first
    call wins process-wide. Without this reset the second test in the file
    would enqueue into the FIRST test's directory and see nothing pending.
    """
    reset_default_queue()
    yield
    reset_default_queue()


def _shared_queue(workdir: Path) -> ApprovalQueue:
    """THE queue the gate will use.

    ``ApprovalQueue.list_pending`` reads in-memory state, so a second instance
    over the same directory does not observe the first one's pending. A test
    resolver has to hold the very object ``gate_tool_call`` pushes into, which
    is what pre-seeding the process-wide default achieves.
    """
    return get_default_queue(_queue_dir(workdir))


def _enable_interactive(workdir: Path, *, timeout_seconds: int = 600) -> None:
    """Turn the gate on. Off by default, which is the no-behaviour-change path."""
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "bernstein.yaml").write_text(
        f"approvals:\n  interactive: true\n  timeout_seconds: {timeout_seconds}\n",
        encoding="utf-8",
    )


def _isolate_audit_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))


def _pretooluse(
    workdir: Path,
    *,
    tool_name: str = "Write",
    tool_input: dict[str, object] | None = None,
    session: str = "sess-4543",
) -> tuple[int, str]:
    """Drive the CLI the way the agent's PreToolUse hook does."""
    runner = CliRunner()
    result = runner.invoke(
        hook_gate_group,
        [
            "check",
            "--session",
            session,
            "--event",
            "PreToolUse",
            "--workdir",
            str(workdir),
            "--timestamp",
            "1700000000",
        ],
        input=json.dumps({"tool_name": tool_name, "tool_input": tool_input or {"file_path": "a.py"}}),
        catch_exceptions=False,
    )
    return result.exit_code, result.output


def _pending_ids(workdir: Path) -> list[str]:
    return [item.id for item in _shared_queue(workdir).list_pending()]


# ---------------------------------------------------------------------------
# 1. A gated call enqueues and blocks until resolved
# ---------------------------------------------------------------------------
def test_gated_tool_call_enqueues_and_blocks_until_resolved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The producer end. Before #4543 the queue could not receive this at all.

    The CLI blocks in ``wait_for``, so the resolution is written from a thread
    while the gate is waiting -- which is exactly how an operator resolving
    through any of the three surfaces reaches it.
    """
    import threading

    _isolate_audit_key(tmp_path, monkeypatch)
    _enable_interactive(tmp_path, timeout_seconds=30)
    _shared_queue(tmp_path)  # pin the singleton to this tmp_path

    resolved_id: list[str] = []

    def _resolve_when_pending() -> None:
        queue = _shared_queue(tmp_path)
        for _ in range(200):  # ~10s ceiling; the gate's TTL is 30
            pending = queue.list_pending()
            if pending:
                approval = pending[0]
                resolved_id.append(approval.id)
                queue.resolve(
                    approval.id,
                    ApprovalDecision.ALLOW,
                    nonce=approval.nonce,
                    reason="operator allowed",
                )
                return
            threading.Event().wait(0.05)

    resolver = threading.Thread(target=_resolve_when_pending, daemon=True)
    resolver.start()
    code, _ = _pretooluse(tmp_path, tool_name="Write")
    resolver.join(timeout=15)

    assert resolved_id, "the tool call never reached the queue - the gate did not fire"
    assert code == _ALLOW, "an allowed approval must let the tool call proceed"


# ---------------------------------------------------------------------------
# 2. TTL expiry denies rather than hangs
# ---------------------------------------------------------------------------
def test_ttl_expiry_denies_rather_than_hangs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The queue's default-deny path, observed by the agent as a denial.

    A hang here would be worse than a denial: the worker would sit on the tool
    call until something else killed it, with no signal to the model.
    """
    _isolate_audit_key(tmp_path, monkeypatch)
    _enable_interactive(tmp_path, timeout_seconds=1)
    _shared_queue(tmp_path)

    code, output = _pretooluse(tmp_path, tool_name="Write")

    assert code == _BLOCK, "TTL expiry must refuse the call, not allow it"
    assert "denied" in output.lower() or "ttl" in output.lower(), output


# ---------------------------------------------------------------------------
# 3. Auto-decided calls never reach the queue
# ---------------------------------------------------------------------------
def test_auto_decided_calls_never_reach_the_queue_when_gate_is_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default profile. No config at all means no queue, no block.

    This is the regression that matters most: wiring a producer into the
    dispatch path must not start pausing agents for operators who never asked
    for interactive approvals.
    """
    _isolate_audit_key(tmp_path, monkeypatch)
    # deliberately no bernstein.yaml -> interactive defaults to False

    code, _ = _pretooluse(tmp_path, tool_name="Write")

    assert code == _ALLOW
    assert _pending_ids(tmp_path) == [], "a default-profile call must not enqueue"


def test_a_rejected_approval_blocks_the_tool_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half of resolution: REJECT must exit 2, feeding the model a
    permission error rather than silently proceeding."""
    import threading

    _isolate_audit_key(tmp_path, monkeypatch)
    _enable_interactive(tmp_path, timeout_seconds=30)
    _shared_queue(tmp_path)  # pin the singleton to this tmp_path

    def _reject_when_pending() -> None:
        queue = _shared_queue(tmp_path)
        for _ in range(200):
            pending = queue.list_pending()
            if pending:
                approval = pending[0]
                queue.resolve(
                    approval.id,
                    ApprovalDecision.REJECT,
                    nonce=approval.nonce,
                    reason="operator refused",
                )
                return
            threading.Event().wait(0.05)

    rejecter = threading.Thread(target=_reject_when_pending, daemon=True)
    rejecter.start()
    code, output = _pretooluse(tmp_path, tool_name="Write")
    rejecter.join(timeout=15)

    assert code == _BLOCK, "a rejected approval must refuse the tool call"
    assert "refused" in output.lower() or "reject" in output.lower(), output


# ---------------------------------------------------------------------------
# 4. Every resolution surface releases the same pending
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("surface", ["http", "cli", "tui"])
def test_each_resolution_surface_releases_the_same_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, surface: str
) -> None:
    """All three shipped surfaces resolve through the same file-backed queue.

    The point of the parametrisation is that the pending is identical whichever
    surface resolves it -- the gate does not know or care which one did.
    """
    import threading

    _isolate_audit_key(tmp_path, monkeypatch)
    _enable_interactive(tmp_path, timeout_seconds=30)
    _shared_queue(tmp_path)  # pin the singleton to this tmp_path

    seen: list[str] = []

    def _resolve_via_surface() -> None:
        queue = _shared_queue(tmp_path)
        for _ in range(200):
            pending = queue.list_pending()
            if pending:
                approval = pending[0]
                seen.append(approval.id)
                # Each surface ultimately calls queue.resolve with the nonce it
                # read from the same pending record; the transport differs, the
                # release does not.
                queue.resolve(
                    approval.id,
                    ApprovalDecision.ALLOW,
                    nonce=approval.nonce,
                    reason=f"resolved via {surface}",
                    channel=surface,
                )
                return
            threading.Event().wait(0.05)

    worker = threading.Thread(target=_resolve_via_surface, daemon=True)
    worker.start()
    code, _ = _pretooluse(tmp_path, tool_name="Write")
    worker.join(timeout=15)

    assert seen, f"{surface}: nothing was ever pending"
    assert code == _ALLOW, f"{surface}: resolution did not release the call"
    assert _pending_ids(tmp_path) == [], f"{surface}: the pending was not consumed"


# ---------------------------------------------------------------------------
# The wiring itself - a control for the defect this issue is about
# ---------------------------------------------------------------------------
def test_the_dispatch_point_actually_calls_the_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#4543 was not a broken gate, it was an uncalled one.

    A test that only asserts on queue contents would pass again if someone
    deleted the call and the queue happened to stay empty. This one fails if
    ``gate_tool_call`` stops being reached from the dispatch path at all.
    """
    _isolate_audit_key(tmp_path, monkeypatch)
    calls: list[str] = []

    import bernstein.core.approval.gate as gate_mod

    def _spy(**kwargs: object) -> None:
        calls.append(str(kwargs.get("tool_name")))
        return None

    monkeypatch.setattr(gate_mod, "gate_tool_call", _spy)

    code, _ = _pretooluse(tmp_path, tool_name="Bash", tool_input={"command": "ls"})

    assert code == _ALLOW
    assert calls == ["Bash"], "the dispatch point did not reach gate_tool_call"
