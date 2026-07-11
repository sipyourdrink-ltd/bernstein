"""``bernstein hook-gate check`` in-process enforcement tests (issue #2360).

The CLI is what a gate-capable adapter wires its PreToolUse / Stop hooks to. It
reads the hook event JSON on stdin, evaluates the persisted policy, seals a
gate receipt into the audit chain, and exits ``2`` to block or ``0`` to allow.
These tests drive the CLI exactly as the worker's hook runner would, proving
AC1 (a failing completion blocks the turn) and AC2 (an out-of-scope write is
refused and appears as a gate receipt in the chain) without a live agent.
"""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING

from click.testing import CliRunner

from bernstein.cli.commands.hook_gate_cmd import hook_gate_group
from bernstein.core.security.hook_gate import (
    HookGatePolicy,
    policy_from_task_fields,
    write_policy,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _isolate_audit_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))


def _run(tmp_path: Path, session: str, event: str, payload: dict[str, object]) -> tuple[int, str]:
    runner = CliRunner()
    result = runner.invoke(
        hook_gate_group,
        ["check", "--session", session, "--event", event, "--workdir", str(tmp_path), "--timestamp", "1700000000"],
        input=json.dumps(payload),
        catch_exceptions=False,
    )
    import contextlib

    combined = result.output or ""
    with contextlib.suppress(ValueError):
        combined += result.stderr or ""
    return result.exit_code, combined


def _events_in_chain(tmp_path: Path) -> list[dict[str, object]]:
    from bernstein.core.security.audit_chain import EVENT_EVIDENCE_BUNDLE, AuditChainStore

    chain = AuditChainStore(tmp_path / ".sdd" / "audit")
    return [dict(e.details) for e in chain.query(event_type=EVENT_EVIDENCE_BUNDLE)]


# ---------------------------------------------------------------------------
# AC2: out-of-scope write refused in-process + gate receipt in the chain
# ---------------------------------------------------------------------------


def test_out_of_scope_write_is_blocked_and_sealed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_audit_key(tmp_path, monkeypatch)
    policy = policy_from_task_fields("T-cli-1", owned_files=["src/**"], evidence_producers=[])
    write_policy(tmp_path, "qa-abc12345", policy)

    exit_code, out = _run(
        tmp_path,
        "qa-abc12345",
        "PreToolUse",
        {"tool_name": "Write", "tool_input": {"file_path": "infra/prod.tf", "content": "x"}},
    )
    assert exit_code == 2
    assert "out-of-scope" in out.lower()

    events = _events_in_chain(tmp_path)
    assert events
    assert any(e.get("gate_passed") is False for e in events)


def test_in_scope_write_is_allowed_without_receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_audit_key(tmp_path, monkeypatch)
    policy = policy_from_task_fields("T-cli-2", owned_files=["src/**"], evidence_producers=[])
    write_policy(tmp_path, "qa-abc12345", policy)

    exit_code, _ = _run(
        tmp_path,
        "qa-abc12345",
        "PreToolUse",
        {"tool_name": "Edit", "tool_input": {"file_path": "src/pkg/mod.py"}},
    )
    assert exit_code == 0
    assert _events_in_chain(tmp_path) == []


# ---------------------------------------------------------------------------
# AC1: a failing completion gate blocks the turn
# ---------------------------------------------------------------------------


def test_failing_completion_blocks_turn_and_seals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_audit_key(tmp_path, monkeypatch)
    policy = HookGatePolicy(
        "T-cli-3",
        producers=policy_from_task_fields(
            "T-cli-3",
            owned_files=[],
            evidence_producers=[
                {
                    "name": "tests",
                    "kind": "test",
                    "command": [sys.executable, "-c", "import sys; sys.exit(1)"],
                    "required": True,
                }
            ],
        ).producers,
    )
    write_policy(tmp_path, "qa-abc12345", policy)

    exit_code, _ = _run(tmp_path, "qa-abc12345", "Stop", {"hook_event_name": "Stop"})
    assert exit_code == 2
    events = _events_in_chain(tmp_path)
    assert any(e.get("gate_passed") is False for e in events)


def test_passing_completion_allows_turn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_audit_key(tmp_path, monkeypatch)
    policy = policy_from_task_fields(
        "T-cli-4",
        owned_files=[],
        evidence_producers=[
            {"name": "tests", "kind": "test", "command": [sys.executable, "-c", "print('ok')"], "required": True}
        ],
    )
    write_policy(tmp_path, "qa-abc12345", policy)

    exit_code, _ = _run(tmp_path, "qa-abc12345", "Stop", {"hook_event_name": "Stop"})
    assert exit_code == 0
    events = _events_in_chain(tmp_path)
    assert any(e.get("gate_passed") is True for e in events)


# ---------------------------------------------------------------------------
# AC4: absent policy degrades to allow-through with no receipt
# ---------------------------------------------------------------------------


def test_missing_policy_allows_through(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_audit_key(tmp_path, monkeypatch)
    exit_code, _ = _run(
        tmp_path,
        "qa-nopolicy",
        "PreToolUse",
        {"tool_name": "Write", "tool_input": {"file_path": "anywhere.py"}},
    )
    assert exit_code == 0
    assert _events_in_chain(tmp_path) == []


def test_invalid_session_id_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_audit_key(tmp_path, monkeypatch)
    # A traversal session id never reaches the filesystem.
    exit_code, _ = _run(
        tmp_path,
        "../../etc",
        "PreToolUse",
        {"tool_name": "Write", "tool_input": {"file_path": "x.py"}},
    )
    # Fail-open on the enforcement decision (allow), but never touch an unsafe path.
    assert exit_code == 0
    assert _events_in_chain(tmp_path) == []
