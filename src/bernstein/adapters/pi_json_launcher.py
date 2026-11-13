"""Run ``pi --mode json`` with pi's own text-mode exit semantics (issue #6943).

The Pi adapter runs pi in JSON mode so the runner log receives events while
the agent works; that log is the only liveness signal pi gives. JSON mode has
one gap: pi decides a nonzero exit only in text mode. ``runPrintMode`` checks
the last assistant message and returns 1 when its ``stopReason`` is
``error``, ``aborted`` or ``length``, but that check sits inside
``if (mode === "text")`` (pi ``packages/coding-agent/src/modes/print-mode.ts``
at ``f9a2636``). In JSON mode the same failed turn exits 0, and bernstein,
which forwards the child's exit status, would read it as a clean finish.

This launcher closes that gap without giving up streaming. It runs pi, copies
every stdout line to its own stdout as soon as it arrives, watches the
``message_end`` events, and on exit applies the text-mode rule to the last
message: an assistant message that stopped on ``error``, ``aborted`` or
``length`` turns a 0 into 1. An ``auto_retry_end`` with ``success: false``
counts as a failure unless a later assistant message ended cleanly. A nonzero
exit from pi itself is passed through unchanged.

Usage::

    python -m bernstein.adapters.pi_json_launcher -- pi --mode json ... -- <prompt>
"""

from __future__ import annotations

import json
import signal
import subprocess
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

#: Printed when the pi binary cannot be started.
PI_INSTALL_HINT = (
    "Install: npm install -g @mariozechner/pi-coding-agent "
    "or see https://github.com/badlogic/pi-mono/tree/main/packages/coding-agent"
)

#: Stop reasons pi's text mode turns into exit code 1.
FAILED_STOP_REASONS = frozenset({"error", "aborted", "length"})


class TerminalOutcome:
    """Tracks the events that decide whether a JSON-mode run failed."""

    def __init__(self) -> None:
        self._last_message: dict[str, Any] | None = None
        self._retry_failure: str | None = None

    def feed(self, line: str) -> None:
        """Consume one stdout line; anything that is not a JSON event is ignored."""
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        if kind == "message_end" and isinstance(event.get("message"), dict):
            message = event["message"]
            self._last_message = message
            if message.get("role") == "assistant" and message.get("stopReason") not in FAILED_STOP_REASONS:
                self._retry_failure = None
        elif kind == "auto_retry_end" and event.get("success") is False:
            self._retry_failure = str(event.get("finalError") or "automatic retries exhausted")

    def failure(self) -> str | None:
        """The reason this run failed, or None when it ended cleanly."""
        message = self._last_message
        if message is not None and message.get("role") == "assistant":
            stop = message.get("stopReason")
            if stop in FAILED_STOP_REASONS:
                detail = message.get("errorMessage") or f"request {stop}"
                return f"final assistant message stopped on {stop!r}: {detail}"
        if self._retry_failure is not None:
            return f"automatic retries failed: {self._retry_failure}"
        return None


def terminal_failure(lines: Iterable[str]) -> str | None:
    """Return why a recorded JSON-mode stream failed, or None if it ended cleanly."""
    outcome = TerminalOutcome()
    for line in lines:
        outcome.feed(line)
    return outcome.failure()


def run(argv: list[str]) -> int:
    """Run ``argv`` (the pi command), streaming its stdout, and return the exit code."""
    try:
        child = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=None, text=True, encoding="utf-8", errors="replace"
        )
    except FileNotFoundError:
        # The adapter launches this module, not pi, so a missing pi lands here.
        print(f"{argv[0]} not found in PATH. {PI_INSTALL_HINT}", file=sys.stderr, flush=True)
        return 127

    def _forward(signum: int, _frame: Any) -> None:
        # Heartbeat escalation and the timeout watchdog signal this process;
        # pi must stop with it rather than outlive it.
        if child.poll() is None:
            child.send_signal(signum)

    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is not None:
            signal.signal(sig, _forward)

    outcome = TerminalOutcome()
    assert child.stdout is not None
    for line in child.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        outcome.feed(line)
    code = child.wait()
    if code != 0:
        return code
    reason = outcome.failure()
    if reason is not None:
        print(f"pi: {reason}", file=sys.stderr, flush=True)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["--"]:
        args = args[1:]
    if not args:
        print("usage: python -m bernstein.adapters.pi_json_launcher -- pi ...", file=sys.stderr)
        return 2
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
