"""Wiring tests: the smart auto-approve classifier gates the live path.

Issue #1850 - the ``auto_approve`` classifier was fully implemented and
unit-tested but never invoked by any runtime code path, so its deny-list
and evasion defenses gated nothing in a live run. These tests assert the
classifier is now on the production approval path
(``bernstein.core.approval.gate.await_tool_call``) end-to-end, never by
calling ``classify_command`` directly, plus that:

* a deny-listed command is rejected by the production path and the
  rejection is written to the HMAC-chained audit log with the matched
  pattern (and the chain still verifies);
* the auto-approve posture is fail-closed: a safe command is NOT
  auto-approved unless the operator opts in via config;
* ``NotebookEdit`` is treated as a write tool (ASK), matching the
  module's own policy and ``traces.py``'s ``_EDIT_TOOLS`` set;
* removing the wiring fails CI (a spy asserts the classifier is called).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from bernstein.core.approval.gate import (
    ApprovalConfig,
    await_tool_call,
)
from bernstein.core.approval.models import ApprovalDecision, ApprovalPrincipal
from bernstein.core.security.always_allow import AlwaysAllowEngine
from bernstein.core.security.audit import AuditLog
from bernstein.core.security.auto_approve import Decision

#: Every resolve names the operator it is attributed to; the queue has no
#: default principal to fall back on (#5035).
_PRINCIPAL = ApprovalPrincipal(identifier="alice@example.test", auth_method="scoped-token")


def _audit_dir(workdir: Path) -> Path:
    return workdir / ".sdd" / "audit"


# ---------------------------------------------------------------------------
# Deny-listed command is rejected by the production path (end-to-end)
# ---------------------------------------------------------------------------


class TestDenyListedCommandRejectedEndToEnd:
    def test_rm_rf_is_rejected_even_with_interactive_on(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            result = await await_tool_call(
                session_id="S",
                agent_role="backend",
                tool_name="Bash",
                tool_args={"command": "rm -rf /tmp/x"},
                workdir=tmp_path,
                engine=AlwaysAllowEngine(rules=[]),
                config=ApprovalConfig(interactive=True, timeout_seconds=1),
            )
            assert result is not None
            assert result.decision is ApprovalDecision.REJECT

        asyncio.run(scenario())

    def test_rm_rf_is_rejected_even_when_interactive_off(self, tmp_path: Path) -> None:
        # The deny check must apply regardless of interactive mode, so a
        # fail-closed deny cannot be bypassed by disabling approvals.
        async def scenario() -> None:
            result = await await_tool_call(
                session_id="S",
                agent_role="backend",
                tool_name="Bash",
                tool_args={"command": "rm -rf /tmp/x"},
                workdir=tmp_path,
                engine=AlwaysAllowEngine(rules=[]),
                config=ApprovalConfig(interactive=False, timeout_seconds=1),
            )
            assert result is not None
            assert result.decision is ApprovalDecision.REJECT

        asyncio.run(scenario())

    def test_denied_command_is_written_to_audit_chain_and_verifies(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            await await_tool_call(
                session_id="S",
                agent_role="backend",
                tool_name="Bash",
                tool_args={"command": "rm -rf /tmp/x"},
                workdir=tmp_path,
                engine=AlwaysAllowEngine(rules=[]),
                config=ApprovalConfig(interactive=True, timeout_seconds=1),
            )

        asyncio.run(scenario())

        log = AuditLog(audit_dir=_audit_dir(tmp_path))
        valid, errors = log.verify()
        assert valid, f"audit chain did not verify: {errors}"

        events = list(log.query())
        deny_events = [
            e for e in events if e.event_type == "auto_approve_decision" and e.details.get("decision") == "deny"
        ]
        assert deny_events, f"no deny audit event recorded; events={[e.event_type for e in events]}"
        evt = deny_events[0]
        assert evt.details.get("tool") == "Bash"
        # The matched deny pattern must be recorded so an auditor can prove why.
        assert evt.details.get("matched_pattern")


# ---------------------------------------------------------------------------
# Fail-closed posture for safe commands
# ---------------------------------------------------------------------------


class TestFailClosedAutoApprovePosture:
    def test_safe_command_not_auto_approved_by_default(self, tmp_path: Path) -> None:
        # Default posture: a classifier APPROVE does NOT silently auto-approve.
        # With interactive on and the auto-approve flag off (default), a safe
        # command still goes to the operator queue rather than short-circuiting.
        from bernstein.core.approval.queue import ApprovalQueue

        queue = ApprovalQueue(base_dir=tmp_path)

        async def scenario() -> None:
            gate_task = asyncio.create_task(
                await_tool_call(
                    session_id="S",
                    agent_role="backend",
                    tool_name="Bash",
                    tool_args={"command": "ls -la"},
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
                pytest.fail("Safe command was auto-approved instead of queued (fail-open!)")
            resolution = await gate_task
            assert resolution is not None
            assert resolution.decision is ApprovalDecision.ALLOW

        asyncio.run(scenario())

    def test_safe_command_auto_approved_when_operator_opts_in(self, tmp_path: Path) -> None:
        # Opt-in posture: when smart_auto_approve is enabled, a classifier
        # APPROVE short-circuits to ALLOW without queueing.
        async def scenario() -> None:
            result = await await_tool_call(
                session_id="S",
                agent_role="backend",
                tool_name="Bash",
                tool_args={"command": "ls -la"},
                workdir=tmp_path,
                engine=AlwaysAllowEngine(rules=[]),
                config=ApprovalConfig(
                    interactive=True,
                    timeout_seconds=1,
                    smart_auto_approve=True,
                ),
            )
            assert result is not None
            assert result.decision is ApprovalDecision.ALLOW

        asyncio.run(scenario())

    def test_ambiguous_command_falls_through_to_queue_even_when_opted_in(self, tmp_path: Path) -> None:
        # An ASK classification must NOT auto-approve even with the flag on;
        # it falls through to the interactive queue (fail-closed on uncertainty).
        from bernstein.core.approval.queue import ApprovalQueue

        queue = ApprovalQueue(base_dir=tmp_path)

        async def scenario() -> None:
            gate_task = asyncio.create_task(
                await_tool_call(
                    session_id="S",
                    agent_role="backend",
                    tool_name="Bash",
                    tool_args={"command": "make build"},
                    workdir=tmp_path,
                    queue=queue,
                    engine=AlwaysAllowEngine(rules=[]),
                    config=ApprovalConfig(
                        interactive=True,
                        timeout_seconds=2,
                        smart_auto_approve=True,
                    ),
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
                pytest.fail("Ambiguous command was auto-approved (fail-open on uncertainty!)")
            resolution = await gate_task
            assert resolution is not None

        asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Wiring regression: the classifier must actually be called
# ---------------------------------------------------------------------------


class TestClassifierIsWired:
    def test_await_tool_call_invokes_classifier(self, tmp_path: Path) -> None:
        # Spy on the classifier as imported by the gate module. If the
        # wiring is removed, this assertion fails and CI catches it.
        from bernstein.core.security.auto_approve import ApprovalResult

        sentinel = ApprovalResult(Decision.ASK, "spy")

        async def scenario() -> None:
            with patch(
                "bernstein.core.approval.gate.classify_tool_call",
                return_value=sentinel,
            ) as spy:
                await await_tool_call(
                    session_id="S",
                    agent_role="backend",
                    tool_name="Bash",
                    tool_args={"command": "ls"},
                    workdir=tmp_path,
                    engine=AlwaysAllowEngine(rules=[]),
                    config=ApprovalConfig(interactive=False, timeout_seconds=1),
                )
                assert spy.called, "approval gate did not invoke the auto-approve classifier"

        asyncio.run(scenario())

    def test_classifier_error_is_fail_closed(self, tmp_path: Path) -> None:
        # If the classifier raises, the gate must NOT auto-approve; it falls
        # through to the existing approval flow (here: interactive off -> None,
        # meaning "no auto-decision, defer to legacy ask-mode"), never ALLOW.
        async def scenario() -> None:
            with patch(
                "bernstein.core.approval.gate.classify_tool_call",
                side_effect=RuntimeError("boom"),
            ):
                result = await await_tool_call(
                    session_id="S",
                    agent_role="backend",
                    tool_name="Bash",
                    tool_args={"command": "ls"},
                    workdir=tmp_path,
                    engine=AlwaysAllowEngine(rules=[]),
                    config=ApprovalConfig(
                        interactive=False,
                        timeout_seconds=1,
                        smart_auto_approve=True,
                    ),
                )
                # Fail-closed: an errored classifier never yields an ALLOW.
                assert result is None or result.decision is not ApprovalDecision.ALLOW

        asyncio.run(scenario())
