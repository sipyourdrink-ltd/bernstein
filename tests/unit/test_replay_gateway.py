"""Unit tests for ``bernstein.core.replay`` - gateway + diff.

Covers the MVP slice of issue #1319:

* record / replay round-trip through the gateway against the ``mock``
  adapter response shape;
* opt-in semantics (``BERNSTEIN_RECORD`` env var, ``record=True`` flag);
* divergence finder: identical runs, length mismatch, value mismatch.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
from pathlib import Path

import pytest

from bernstein.core.replay import (
    EVENTS_FILENAME,
    RECORD_ENV_VAR,
    GatewayMode,
    ReplayGateway,
    ReplayMissError,
    diff_event_logs,
    is_recording_enabled,
)

# ---------------------------------------------------------------------------
# is_recording_enabled
# ---------------------------------------------------------------------------


def test_is_recording_enabled_default_off() -> None:
    """Recording must be OFF when the env var is unset."""
    assert is_recording_enabled(env={}) is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_is_recording_enabled_truthy(value: str) -> None:
    assert is_recording_enabled(env={RECORD_ENV_VAR: value}) is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_is_recording_enabled_falsy(value: str) -> None:
    assert is_recording_enabled(env={RECORD_ENV_VAR: value}) is False


# ---------------------------------------------------------------------------
# Gateway default mode
# ---------------------------------------------------------------------------


def test_gateway_defaults_to_off_when_env_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(RECORD_ENV_VAR, raising=False)
    gw = ReplayGateway("run-1", tmp_path)
    assert gw.mode is GatewayMode.OFF
    # No file should be created when recording is off.
    assert not gw.path.exists()


def test_gateway_env_opt_in_enables_recording(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(RECORD_ENV_VAR, "1")
    gw = ReplayGateway("run-2", tmp_path)
    assert gw.mode is GatewayMode.RECORD


def test_gateway_explicit_record_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(RECORD_ENV_VAR, raising=False)
    gw = ReplayGateway("run-3", tmp_path, record=True)
    assert gw.mode is GatewayMode.RECORD


# ---------------------------------------------------------------------------
# OFF mode passes through
# ---------------------------------------------------------------------------


def test_off_mode_is_passthrough(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(RECORD_ENV_VAR, raising=False)
    gw = ReplayGateway("run-off", tmp_path)
    calls: list[int] = []

    def _invoke() -> str:
        calls.append(1)
        return "live"

    out = gw.dispatch(kind="llm", key="k1", invoke=_invoke)
    assert out == "live"
    assert calls == [1]
    assert not gw.path.exists()


# ---------------------------------------------------------------------------
# Record/replay round-trip - mock adapter shape
# ---------------------------------------------------------------------------


def _mock_llm_response(prompt: str) -> dict[str, object]:
    """Stand-in for what the ``mock`` adapter would emit on a prompt."""
    return {"prompt": prompt, "completion": f"echo:{prompt}", "tokens": len(prompt)}


def test_record_then_replay_produces_identical_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Record a run against the mock-style adapter, then replay it.

    Replay must return byte-identical responses without ever invoking
    the underlying callable.
    """
    monkeypatch.setenv(RECORD_ENV_VAR, "1")

    # --- record phase ---------------------------------------------------
    rec = ReplayGateway("run-rt", tmp_path)
    assert rec.mode is GatewayMode.RECORD

    recorded = [
        rec.dispatch(
            kind="llm",
            key=f"llm-{i}",
            invoke=lambda i=i: _mock_llm_response(f"prompt-{i}"),
        )
        for i in range(3)
    ]
    tool_recorded = rec.dispatch(
        kind="tool",
        key="run_tests",
        invoke=lambda: {"exit": 0, "stdout": "OK"},
    )

    assert rec.path.exists()
    lines = rec.path.read_text().splitlines()
    assert len(lines) == 4
    # Each line is valid JSON with the expected fields.
    for raw in lines:
        row = json.loads(raw)
        assert {"seq", "ts", "kind", "key", "response"} <= row.keys()

    # --- replay phase ---------------------------------------------------
    monkeypatch.delenv(RECORD_ENV_VAR, raising=False)
    replay = ReplayGateway("run-rt", tmp_path, mode=GatewayMode.REPLAY)
    assert replay.mode is GatewayMode.REPLAY

    def _explode() -> object:
        raise AssertionError("replay must not call invoke()")

    replayed = [replay.dispatch(kind="llm", key=f"llm-{i}", invoke=_explode) for i in range(3)]
    tool_replayed = replay.dispatch(kind="tool", key="run_tests", invoke=_explode)

    assert replayed == recorded
    assert tool_replayed == tool_recorded


def test_replay_falls_back_to_fifo_when_key_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the caller's key changed between record and replay, the
    gateway should still serve responses FIFO-by-kind so jittery hash
    inputs don't break replay."""
    monkeypatch.setenv(RECORD_ENV_VAR, "1")
    rec = ReplayGateway("run-fifo", tmp_path)
    rec.dispatch(kind="llm", key="orig-key", invoke=lambda: "first")
    rec.dispatch(kind="llm", key="orig-key-2", invoke=lambda: "second")

    replay = ReplayGateway("run-fifo", tmp_path, mode=GatewayMode.REPLAY)
    # Caller uses *different* keys this time around.
    first = replay.dispatch(kind="llm", key="new-key", invoke=lambda: "WRONG")
    second = replay.dispatch(kind="llm", key="new-key-2", invoke=lambda: "WRONG")
    assert first == "first"
    assert second == "second"


def test_replay_miss_raises_when_no_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(RECORD_ENV_VAR, "1")
    rec = ReplayGateway("run-miss", tmp_path)
    rec.dispatch(kind="llm", key="k", invoke=lambda: "x")

    replay = ReplayGateway("run-miss", tmp_path, mode=GatewayMode.REPLAY)
    replay.dispatch(kind="llm", key="k", invoke=lambda: "WRONG")
    with pytest.raises(ReplayMissError):
        replay.dispatch(kind="llm", key="k", invoke=lambda: "WRONG")


def test_replay_missing_events_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ReplayMissError):
        ReplayGateway("nope", tmp_path, mode=GatewayMode.REPLAY)


# ---------------------------------------------------------------------------
# diff_event_logs
# ---------------------------------------------------------------------------


def _write_events(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def test_diff_identical_runs(tmp_path: Path) -> None:
    a = tmp_path / "a" / EVENTS_FILENAME
    b = tmp_path / "b" / EVENTS_FILENAME
    rows = [
        {"seq": 1, "kind": "llm", "key": "k1", "response": "x"},
        {"seq": 2, "kind": "tool", "key": "t1", "response": {"ok": True}},
    ]
    _write_events(a, rows)
    _write_events(b, rows)
    result = diff_event_logs(a, b)
    assert result.diverged is False
    assert result.index is None
    assert "2 events match" in result.reason


def test_diff_value_mismatch(tmp_path: Path) -> None:
    a = tmp_path / "a" / EVENTS_FILENAME
    b = tmp_path / "b" / EVENTS_FILENAME
    _write_events(
        a,
        [
            {"seq": 1, "kind": "llm", "key": "k", "response": "alpha"},
            {"seq": 2, "kind": "llm", "key": "k", "response": "beta"},
        ],
    )
    _write_events(
        b,
        [
            {"seq": 1, "kind": "llm", "key": "k", "response": "alpha"},
            {"seq": 2, "kind": "llm", "key": "k", "response": "GAMMA"},
        ],
    )
    result = diff_event_logs(a, b)
    assert result.diverged is True
    assert result.index == 1
    assert result.a_event is not None and result.a_event["response"] == "beta"
    assert result.b_event is not None and result.b_event["response"] == "GAMMA"


def test_diff_length_mismatch(tmp_path: Path) -> None:
    a = tmp_path / "a" / EVENTS_FILENAME
    b = tmp_path / "b" / EVENTS_FILENAME
    _write_events(
        a,
        [
            {"seq": 1, "kind": "llm", "key": "k", "response": "x"},
            {"seq": 2, "kind": "llm", "key": "k", "response": "y"},
        ],
    )
    _write_events(
        b,
        [
            {"seq": 1, "kind": "llm", "key": "k", "response": "x"},
        ],
    )
    result = diff_event_logs(a, b)
    assert result.diverged is True
    assert result.index == 1
    assert "extra event" in result.reason


def test_diff_both_empty(tmp_path: Path) -> None:
    a = tmp_path / "a" / EVENTS_FILENAME
    b = tmp_path / "b" / EVENTS_FILENAME
    a.parent.mkdir(parents=True)
    b.parent.mkdir(parents=True)
    a.touch()
    b.touch()
    result = diff_event_logs(a, b)
    assert result.diverged is False
    assert "empty" in result.reason


# ---------------------------------------------------------------------------
# Regression: concurrent record/replay must produce well-formed JSONL
# ---------------------------------------------------------------------------


def test_record_is_thread_safe_under_concurrent_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent ``dispatch`` calls must produce well-formed JSONL with
    unique ``seq`` values, and replay must be able to drain every event.

    Regression guard for the gateway recording race: ``seq`` assignment
    used to happen under the lock but the JSON encode and file open/write
    were outside it. POSIX ``O_APPEND`` masks the race for short writes
    today, but a future writer (or a switch to buffered I/O) would let
    bytes interleave past one ``write()`` syscall. Keeping seq + write
    inside the same lock pins the invariant the replay loader depends on.
    """
    monkeypatch.setenv(RECORD_ENV_VAR, "1")
    gw = ReplayGateway("run-race", tmp_path)

    # A response just over typical PIPE_BUF (4 KiB on Linux/macOS) so an
    # unlocked write path would expose interleaving more reliably. The
    # body is reproducible per thread index for later assertions.
    big = "X" * 6000

    def _worker(i: int) -> None:
        gw.dispatch(
            kind="llm",
            key=f"k-{i}",
            invoke=lambda i=i: {"i": i, "body": big},
        )

    # A bounded pool rather than 40 raw threads: a loaded runner refuses to
    # start that many, which fails the run for a reason that has nothing to
    # do with the gateway. Several writers on the lock at once is what the
    # invariant needs, and the pool still delivers that.
    workers = min(40, (os.cpu_count() or 2) * 4)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_worker, i) for i in range(40)]

    # result() re-raises a worker's exception here. Without it a failing
    # worker would surface only as the line-count mismatch below, which
    # names neither the failure nor the thread it happened on.
    for fut in futures:
        fut.result()

    # Every line must parse and every seq must be unique + monotonic.
    raw_lines = gw.path.read_text(encoding="utf-8").splitlines()
    assert len(raw_lines) == 40
    rows = [json.loads(line) for line in raw_lines]
    seqs = [int(r["seq"]) for r in rows]
    assert sorted(seqs) == list(range(1, 41)), "seq numbers must be unique 1..N"

    # Replay must drain all 40 fixtures by FIFO without raising.
    replay = ReplayGateway("run-race", tmp_path, mode=GatewayMode.REPLAY)
    seen: set[int] = set()
    for _ in range(40):
        out = replay.dispatch(
            kind="llm",
            key="any",  # force the FIFO fallback
            invoke=lambda: (_ for _ in ()).throw(AssertionError("invoke must not run")),
        )
        seen.add(int(out["i"]))
    assert seen == set(range(40))
