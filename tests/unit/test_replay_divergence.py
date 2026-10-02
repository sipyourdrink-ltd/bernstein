"""Same-scheme replay key miss is a divergence and does not consume (#4866)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from bernstein.core.replay import (
    LENIENT_ENV_VAR,
    RECORD_ENV_VAR,
    GatewayMode,
    ReplayDivergenceError,
    ReplayGateway,
    ReplayMissError,
    derive_replay_key,
)


def _explode() -> object:
    raise AssertionError("replay must not call invoke()")


def _record_two(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(RECORD_ENV_VAR, "1")
    rec = ReplayGateway("run-div", tmp_path)
    rec.dispatch(kind="llm", key="orig", invoke=lambda: "first")
    rec.dispatch(kind="llm", key="orig-2", invoke=lambda: "second")


def test_no_recorded_events_refuses(tmp_path: Path) -> None:
    """Outcome 1: a run with no events log refuses before any live call."""
    with pytest.raises(ReplayMissError, match="nothing to replay"):
        ReplayGateway("run-empty", tmp_path, mode=GatewayMode.REPLAY)


def test_key_hit_is_byte_identical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Outcome 2: an exact key returns the recorded response, twice."""
    _record_two(tmp_path, monkeypatch)

    def _drain() -> list[object]:
        gw = ReplayGateway("run-div", tmp_path, mode=GatewayMode.REPLAY)
        return [
            gw.dispatch(kind="llm", key="orig", invoke=_explode),
            gw.dispatch(kind="llm", key="orig-2", invoke=_explode),
        ]

    assert _drain() == ["first", "second"]
    assert _drain() == ["first", "second"]


def test_key_miss_names_next_key_and_does_not_consume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Outcome 3: a drifted key raises and leaves the queue index untouched."""
    _record_two(tmp_path, monkeypatch)
    gw = ReplayGateway("run-div", tmp_path, mode=GatewayMode.REPLAY)
    before_cursor = gw._kind_cursor.get("llm", 0)
    before_flags = [fixture.consumed for fixture in gw._ordered_by_kind["llm"]]

    with pytest.raises(ReplayDivergenceError) as excinfo:
        gw.dispatch(kind="llm", key="drifted", invoke=_explode)

    err = excinfo.value
    assert err.kind == "llm"
    assert err.got_key == derive_replay_key("drifted")
    assert err.expected_key == derive_replay_key("orig")
    assert err.got_key.startswith("v1:")
    assert err.expected_key.startswith("v1:")
    assert err.got_key in str(err)
    assert err.expected_key in str(err)
    assert gw._kind_cursor.get("llm", 0) == before_cursor
    assert [fixture.consumed for fixture in gw._ordered_by_kind["llm"]] == before_flags
    # The recorded key still serves the fixture the miss must not have taken.
    assert gw.dispatch(kind="llm", key="orig", invoke=_explode) == "first"


def test_exhausted_recording_names_the_variant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Outcome 4: a miss with nothing left is the exhausted divergence."""
    _record_two(tmp_path, monkeypatch)
    gw = ReplayGateway("run-div", tmp_path, mode=GatewayMode.REPLAY)
    assert gw.dispatch(kind="llm", key="orig", invoke=_explode) == "first"
    assert gw.dispatch(kind="llm", key="orig-2", invoke=_explode) == "second"
    before_flags = [fixture.consumed for fixture in gw._ordered_by_kind["llm"]]

    with pytest.raises(ReplayDivergenceError) as excinfo:
        gw.dispatch(kind="llm", key="extra", invoke=_explode)

    assert excinfo.value.expected_key is None
    assert excinfo.value.got_key == derive_replay_key("extra")
    assert "recording exhausted" in str(excinfo.value)
    assert [fixture.consumed for fixture in gw._ordered_by_kind["llm"]] == before_flags


def test_missing_kind_is_exhausted_when_other_kinds_exist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _record_two(tmp_path, monkeypatch)
    gw = ReplayGateway("run-div", tmp_path, mode=GatewayMode.REPLAY)
    with pytest.raises(ReplayDivergenceError) as excinfo:
        gw.dispatch(kind="tool", key="run_tests", invoke=_explode)
    assert excinfo.value.kind == "tool"
    assert excinfo.value.expected_key is None
    assert [fixture.consumed for fixture in gw._ordered_by_kind["llm"]] == [False, False]


def test_lenient_fifo_logs_one_line_per_consumption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _record_two(tmp_path, monkeypatch)
    gw = ReplayGateway("run-div", tmp_path, mode=GatewayMode.REPLAY, lenient=True)
    with caplog.at_level(logging.WARNING):
        first = gw.dispatch(kind="llm", key="drifted", invoke=_explode)
        second = gw.dispatch(kind="llm", key="drifted-2", invoke=_explode)
    assert first == "first"
    assert second == "second"
    fallbacks = [rec for rec in caplog.records if LENIENT_ENV_VAR in rec.getMessage()]
    assert len(fallbacks) == 2
    assert derive_replay_key("orig") in fallbacks[0].getMessage()


def test_lenient_env_var_restores_fifo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _record_two(tmp_path, monkeypatch)
    monkeypatch.setenv(LENIENT_ENV_VAR, "1")
    gw = ReplayGateway("run-div", tmp_path, mode=GatewayMode.REPLAY)
    assert gw.dispatch(kind="llm", key="drifted", invoke=_explode) == "first"
