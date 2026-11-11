"""``pi_json_launcher`` streams pi's JSON events and restores text mode's exit code (#6943).

pi decides a nonzero exit only in text mode: ``runPrintMode`` returns 1 when
the last assistant message stopped on ``error``, ``aborted`` or ``length``,
inside ``if (mode === "text")``. JSON mode, which the adapter needs for a
growing log, exits 0 after the same failed turn. These tests drive the real
launcher over a fake ``pi`` that writes the documented event shapes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import bernstein
from bernstein.adapters.pi_json_launcher import terminal_failure

_SRC = str(Path(bernstein.__file__).resolve().parents[1])


def _assistant_end(stop: str, error: str | None = None) -> dict[str, object]:
    message: dict[str, object] = {"role": "assistant", "stopReason": stop, "content": []}
    if error is not None:
        message["errorMessage"] = error
    return {"type": "message_end", "message": message}


def _lines(*events: dict[str, object]) -> list[str]:
    return [json.dumps(event) + "\n" for event in events]


# ---------------------------------------------------------------------------
# The decision itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stop", ["error", "aborted", "length"])
def test_a_failed_final_assistant_message_is_a_failure(stop: str) -> None:
    reason = terminal_failure(_lines({"type": "agent_start"}, _assistant_end(stop, error="boom")))
    assert reason is not None
    assert repr(stop) in reason


@pytest.mark.parametrize("stop", ["stop", "toolUse"])
def test_a_clean_final_assistant_message_is_success(stop: str) -> None:
    assert terminal_failure(_lines(_assistant_end("error"), _assistant_end(stop))) is None


def test_only_the_last_message_counts_as_in_text_mode() -> None:
    """Text mode inspects the last message only; a non-assistant last message is not a failure."""
    tool_result = {"type": "message_end", "message": {"role": "toolResult", "content": []}}
    assert terminal_failure(_lines(_assistant_end("error"), tool_result)) is None


def test_exhausted_retries_fail_unless_a_later_turn_succeeds() -> None:
    exhausted = {"type": "auto_retry_end", "success": False, "attempt": 3, "finalError": "529 overloaded"}
    reason = terminal_failure(_lines(exhausted))
    assert reason is not None
    assert "529 overloaded" in reason
    assert terminal_failure(_lines(exhausted, _assistant_end("stop"))) is None


def test_lines_that_are_not_json_events_are_ignored() -> None:
    assert terminal_failure(["not json\n", "[1, 2]\n", "\n", *_lines(_assistant_end("stop"))]) is None


# ---------------------------------------------------------------------------
# The launcher end to end, over a fake pi
# ---------------------------------------------------------------------------

_FAKE_PI = """
import json, os, sys, time
outcome, gate = sys.argv[1], sys.argv[2]
def emit(event):
    sys.stdout.write(json.dumps(event) + "\\n")
    sys.stdout.flush()
emit({"type": "agent_start"})
emit({"type": "turn_start"})
deadline = time.monotonic() + 20
while gate != "-" and not os.path.exists(gate) and time.monotonic() < deadline:
    time.sleep(0.05)
if outcome == "crash":
    sys.exit(3)
stop = {"ok": "stop"}.get(outcome, outcome)
emit({"type": "message_end", "message": {"role": "assistant", "stopReason": stop, "content": [],
      "errorMessage": "provider said no"}})
emit({"type": "agent_end", "messages": [], "willRetry": False})
"""


def _launch(tmp_path: Path, outcome: str, gate: str = "-") -> tuple[subprocess.Popen[bytes], Path]:
    fake = tmp_path / "fake_pi.py"
    fake.write_text(_FAKE_PI, encoding="utf-8")
    log = tmp_path / "agent.log"
    env = {**os.environ, "PYTHONPATH": _SRC + os.pathsep + os.environ.get("PYTHONPATH", "")}
    with log.open("wb") as sink:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "bernstein.adapters.pi_json_launcher",
                "--",
                sys.executable,
                str(fake),
                outcome,
                gate,
            ],
            stdout=sink,
            stderr=subprocess.PIPE,
            env=env,
        )
    return proc, log


@pytest.mark.parametrize(("outcome", "code"), [("ok", 0), ("error", 1), ("aborted", 1), ("length", 1), ("crash", 3)])
def test_launcher_exit_code_follows_the_final_turn(tmp_path: Path, outcome: str, code: int) -> None:
    proc, log = _launch(tmp_path, outcome)
    _, stderr = proc.communicate(timeout=60)
    assert proc.returncode == code, stderr
    events = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert events[0] == {"type": "agent_start"}
    if code == 1:
        assert b"provider said no" in stderr


def test_the_log_grows_while_pi_is_still_working_and_a_late_failure_still_fails(tmp_path: Path) -> None:
    """The liveness half and the exit half together: events reach the log before the
    turn ends, and a run that was alive for its whole duration still exits 1 when its
    final turn failed, instead of reading as a quiet success."""
    gate = tmp_path / "finish"
    proc, log = _launch(tmp_path, "error", str(gate))
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if '"agent_start"' in log.read_text(encoding="utf-8"):
                break
            time.sleep(0.05)
        assert '"agent_start"' in log.read_text(encoding="utf-8"), "no event reached the log before the turn ended"
        assert proc.poll() is None, "the fake pi should still be mid-turn"
        gate.write_text("go", encoding="utf-8")
        _, stderr = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 1, stderr
    assert '"message_end"' in log.read_text(encoding="utf-8")


def test_a_missing_pi_exits_127_with_the_install_hint(tmp_path: Path) -> None:
    """The adapter launches Python, so a missing pi is reported by the launcher, not by Popen."""
    env = {**os.environ, "PYTHONPATH": _SRC + os.pathsep + os.environ.get("PYTHONPATH", "")}
    missing = str(tmp_path / "no-such-pi")
    done = subprocess.run(
        [sys.executable, "-m", "bernstein.adapters.pi_json_launcher", "--", missing, "--mode", "json"],
        capture_output=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert done.returncode == 127
    assert b"npm install -g @mariozechner/pi-coding-agent" in done.stderr
