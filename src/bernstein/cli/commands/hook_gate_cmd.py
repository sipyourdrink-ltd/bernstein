"""``bernstein hook-gate``: in-process verification gate for worker hooks (#2360).

A gate-capable adapter wires its worker's PreToolUse and Stop hooks to
``bernstein hook-gate check``. The command reads the hook event JSON on stdin,
loads the task's persisted policy, and enforces it in-session:

* **PreToolUse** -- an out-of-scope write (a Write/Edit target outside the
  task's path allowlist) is refused; the refusal is sealed as a gate receipt
  and the command exits ``2`` so the tool call never runs.
* **Stop** -- the task's required verification producers run in-session; the
  attempt is sealed as a proof-of-done receipt and the command exits ``2`` when
  a required check failed, so the worker cannot end its turn on red.

The receipt IS an evidence bundle (issue #2362), anchored in the HMAC audit
chain, so a downstream verifier cannot tell from the schema whether the gate
fired here or scheduler-side. The scheduler-side gate stays authoritative and
runs regardless; the in-process gate is defence in depth. Enforcement is
fail-open on unexpected errors -- a bug in sealing must never wedge a worker --
but a computed block is always emitted.

    bernstein hook-gate check --session <id> --event PreToolUse
    bernstein hook-gate check --session <id> --event Stop
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import click

logger = logging.getLogger(__name__)

_ALLOW = 0
_BLOCK = 2


@click.group("hook-gate")
def hook_gate_group() -> None:
    """In-process verification gate driven by worker hooks.

    \b
      bernstein hook-gate check --session <id> --event PreToolUse
      bernstein hook-gate check --session <id> --event Stop
    """


@hook_gate_group.command("check")
@click.option("--session", "session_id", required=True, help="Worker session id (policy key).")
@click.option(
    "--event",
    "event",
    required=True,
    type=click.Choice(["PreToolUse", "Stop"]),
    help="Hook event to evaluate.",
)
@click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False),
    default=".",
    show_default=True,
    help="Worktree root containing .sdd/ (defaults to the hook's cwd).",
)
@click.option(
    "--timestamp",
    type=int,
    default=None,
    help="Override the receipt seal timestamp (deterministic replay/tests).",
)
def hook_gate_check_cmd(session_id: str, event: str, workdir: str, timestamp: int | None) -> None:
    """Evaluate one hook event in-session and block or allow.

    Reads the hook event JSON on stdin. Exits 2 to block (refuse the tool call
    or refuse to end the turn), 0 to allow. A missing or unreadable policy, an
    unsafe session id, or an unexpected error degrades to allow-through so the
    authoritative scheduler-side gate stays the sole enforcement point.
    """
    from bernstein.core.security.hook_gate import (
        evaluate_completion_gate,
        evaluate_path_gate,
        read_policy,
        seal_gate_receipt,
    )
    from bernstein.core.server.hooks_receiver import InvalidSessionIdError, validate_session_id

    root = Path(workdir)

    # An unsafe session id must never reach the filesystem; degrade to allow.
    try:
        validate_session_id(session_id)
    except InvalidSessionIdError:
        logger.warning("hook-gate: rejected unsafe session id; allowing through")
        raise SystemExit(_ALLOW) from None

    policy = read_policy(root, session_id)
    if policy is None or not policy.is_active:
        # AC4: no policy -> degrade to the scheduler-side gate, no weakening.
        raise SystemExit(_ALLOW)

    payload = _read_stdin_json()
    ts = timestamp if timestamp is not None else int(_now())

    if event == "PreToolUse":
        tool_name = str(payload.get("tool_name", ""))
        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, dict):
            tool_input = {}
        outcome = evaluate_path_gate(policy, tool_name=tool_name, tool_input=tool_input, workdir=root)
        if outcome is None or not outcome.blocked:
            raise SystemExit(_ALLOW)
        receipt_task_id = f"{policy.task_id}#gate:pretooluse:{ts}"
    else:  # Stop
        outcome = evaluate_completion_gate(policy, workdir=root)
        receipt_task_id = f"{policy.task_id}#gate:completion:{ts}"

    # Seal the receipt (fail-open on seal errors: the decision still stands).
    try:
        seal_gate_receipt(
            workdir=root,
            task_id=receipt_task_id,
            outcomes=outcome.outcomes,
            timestamp=ts,
        )
    except Exception as exc:  # sealing must never wedge the worker
        # Credential-adjacent path (touches the audit HMAC key): log the
        # exception type only, never its text.
        logger.warning("hook-gate: receipt seal skipped, exception type %s", type(exc).__name__)

    # Stream the decision into the JSONL sidecar the orchestrator ingests,
    # linked to the receipt it sealed (event-to-receipt mapping in ingestion).
    try:
        from bernstein.core.server.hooks_receiver import write_gate_decision_event

        write_gate_decision_event(
            session_id,
            root,
            gate_event=outcome.event,
            blocked=outcome.blocked,
            reason=outcome.reason,
            receipt_task_id=receipt_task_id,
        )
    except Exception as exc:  # observability must never wedge the worker
        logger.debug("hook-gate: gate decision sidecar skipped, exception type %s", type(exc).__name__)

    if outcome.blocked:
        # stderr is fed back to the model; exit 2 refuses the action.
        click.echo(outcome.reason or "hook-gate: blocked", err=True)
        raise SystemExit(_BLOCK)
    raise SystemExit(_ALLOW)


def _read_stdin_json() -> dict[str, object]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _now() -> float:
    import time

    return time.time()
