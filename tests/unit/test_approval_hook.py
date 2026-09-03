"""Unit tests for the security-layer hook that enqueues approvals (op-002).

These tests target ``bernstein.core.approval.gate`` - the glue that sits
between the always-allow engine and the approval queue.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from bernstein.core.approval.gate import (
    ApprovalConfig,
    await_tool_call,
    load_approval_config,
)
from bernstein.core.approval.models import ApprovalDecision, ApprovalPrincipal, ApprovalTimeoutError
from bernstein.core.approval.queue import ApprovalQueue
from bernstein.core.security.always_allow import AlwaysAllowEngine, AlwaysAllowRule

#: Every resolve names the operator it is attributed to; the queue has no
#: default principal to fall back on (#5035).
_PRINCIPAL = ApprovalPrincipal(identifier="alice@example.test", auth_method="scoped-token")


def test_load_approval_config_defaults_when_missing(tmp_path: Path) -> None:
    cfg = load_approval_config(tmp_path)
    assert cfg.interactive is False
    assert cfg.timeout_seconds == 600


def test_load_approval_config_reads_yaml_block(tmp_path: Path) -> None:
    yaml_path = tmp_path / "bernstein.yaml"
    yaml_path.write_text(
        "approvals:\n  interactive: true\n  timeout_seconds: 42\n",
        encoding="utf-8",
    )
    cfg = load_approval_config(tmp_path)
    assert cfg.interactive is True
    assert cfg.timeout_seconds == 42


def test_gate_no_op_when_disabled(tmp_path: Path) -> None:
    queue = ApprovalQueue(base_dir=tmp_path)

    async def scenario() -> None:
        result = await await_tool_call(
            session_id="S",
            agent_role="backend",
            tool_name="shell",
            tool_args={"command": "ls"},
            workdir=tmp_path,
            queue=queue,
            engine=AlwaysAllowEngine(rules=[]),
            config=ApprovalConfig(interactive=False, timeout_seconds=1),
        )
        assert result is None
        assert queue.list_pending() == []

    asyncio.run(scenario())


def test_gate_short_circuits_when_allow_list_matches(tmp_path: Path) -> None:
    engine = AlwaysAllowEngine(
        rules=[
            AlwaysAllowRule(
                id="aa-shell-ls",
                tool="shell",
                input_pattern="ls*",
                input_field="command",
            )
        ]
    )
    queue = ApprovalQueue(base_dir=tmp_path)

    async def scenario() -> None:
        result = await await_tool_call(
            session_id="S",
            agent_role="backend",
            tool_name="shell",
            tool_args={"command": "ls -la"},
            workdir=tmp_path,
            queue=queue,
            engine=engine,
            config=ApprovalConfig(interactive=True, timeout_seconds=1),
        )
        assert result is None
        assert queue.list_pending() == []

    asyncio.run(scenario())


def test_gate_pushes_when_allow_list_misses(tmp_path: Path) -> None:
    queue = ApprovalQueue(base_dir=tmp_path)

    # Use a command that misses both the always-allow rules and the smart
    # classifier's allow/deny lists, so it is genuinely ambiguous (ASK) and
    # reaches the operator queue. A deny-listed command (e.g. ``rm -rf /``)
    # is now rejected outright by the wired classifier and never queued.
    async def scenario() -> None:
        gate_task = asyncio.create_task(
            await_tool_call(
                session_id="S-9",
                agent_role="backend",
                tool_name="shell",
                tool_args={"command": "make build"},
                workdir=tmp_path,
                queue=queue,
                engine=AlwaysAllowEngine(rules=[]),
                config=ApprovalConfig(interactive=True, timeout_seconds=2),
            )
        )

        for _ in range(50):
            pending = queue.list_pending()
            if pending:
                queue.resolve(pending[0].id, ApprovalDecision.ALLOW, principal=_PRINCIPAL)
                break
            await asyncio.sleep(0.01)
        else:
            gate_task.cancel()
            pytest.fail("Gate never pushed onto the queue")

        resolution = await gate_task
        assert resolution is not None
        assert resolution.decision is ApprovalDecision.ALLOW

    asyncio.run(scenario())


def test_gate_raises_timeout_when_unresolved(tmp_path: Path) -> None:
    queue = ApprovalQueue(base_dir=tmp_path)

    # ``make build`` is ambiguous (not allow-listed, not deny-listed) so it
    # queues and then times out. A deny-listed command would instead be
    # rejected immediately by the wired classifier and never time out.
    async def scenario() -> None:
        with pytest.raises(ApprovalTimeoutError):
            await await_tool_call(
                session_id="S",
                agent_role="backend",
                tool_name="shell",
                tool_args={"command": "make build"},
                workdir=tmp_path,
                queue=queue,
                engine=AlwaysAllowEngine(rules=[]),
                config=ApprovalConfig(interactive=True, timeout_seconds=1),
            )

    asyncio.run(scenario())
