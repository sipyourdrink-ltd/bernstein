"""Unit tests for the orchestration liveness watchdog (#1224)."""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.core.orchestration.watchdog import (
    FEATURE_FLAG_ENV,
    SessionSnapshot,
    classify_prompt,
    is_enabled,
    tick,
)


def _record_calls(captured: list[tuple[str, str]]) -> object:
    """Build a respond callable that captures invocations and returns OK."""

    def _respond(session_id: str, keystroke: str) -> bool:
        captured.append((session_id, keystroke))
        return True

    return _respond


def _read_audit(path: Path) -> list[dict[str, object]]:
    """Read JSONL audit events from ``path`` in chronological order."""
    if not path.exists():
        return []
    events: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))
    return events


# ---------------------------------------------------------------------------
# is_enabled / classify_prompt
# ---------------------------------------------------------------------------


def test_is_enabled_off_by_default() -> None:
    assert is_enabled(env={}) is False


def test_is_enabled_truthy_values() -> None:
    for raw in ("1", "true", "True", "YES", "on"):
        assert is_enabled(env={FEATURE_FLAG_ENV: raw}) is True, raw


def test_classify_prompt_matches_safety_pattern() -> None:
    assert classify_prompt("Continue? [y/N]") == "safety"
    assert classify_prompt("noise\nProceed? [y/N]") == "safety"


def test_classify_prompt_flags_model_question() -> None:
    assert classify_prompt("Which of these two file paths did you mean?") == "model_question"


def test_classify_prompt_model_question_wins_over_safety() -> None:
    # If both lookalike patterns appear, the model question must win -
    # auto-answering a model question is the failure mode this primitive
    # must never hit.
    text = "Which file did you mean?\nContinue? [y/N]"
    assert classify_prompt(text) == "safety"
    text2 = "Continue? [y/N]\nWhich file did you mean"
    assert classify_prompt(text2) == "model_question"


def test_classify_prompt_returns_none_for_blank() -> None:
    assert classify_prompt("") == "none"
    assert classify_prompt("\n\n") == "none"


# ---------------------------------------------------------------------------
# tick - feature gate
# ---------------------------------------------------------------------------


def test_tick_disabled_when_flag_off(tmp_path: Path) -> None:
    captured: list[tuple[str, str]] = []
    snapshot = SessionSnapshot(
        session_id="s1",
        recent_output="Continue? [y/N]",
        is_paused=True,
        approved_prompt_classes=frozenset({"safety"}),
    )
    audit = tmp_path / "watchdog.jsonl"
    result = tick([snapshot], _record_calls(captured), audit, env={})
    assert result.recoveries == ()
    assert captured == []
    assert not audit.exists()


# ---------------------------------------------------------------------------
# tick - recovery action
# ---------------------------------------------------------------------------


def test_tick_auto_answers_safety_prompt(tmp_path: Path) -> None:
    captured: list[tuple[str, str]] = []
    snapshot = SessionSnapshot(
        session_id="s1",
        recent_output="some agent log\nContinue? [y/N]",
        is_paused=True,
        approved_prompt_classes=frozenset({"safety"}),
    )
    audit = tmp_path / "watchdog.jsonl"
    env = {FEATURE_FLAG_ENV: "1"}

    result = tick([snapshot], _record_calls(captured), audit, env=env)

    assert len(result.recoveries) == 1
    recovery = result.recoveries[0]
    assert recovery.rule == "prompt_waiting.safety"
    assert recovery.action == "auto_answer:y"
    assert recovery.session_id == "s1"
    assert captured == [("s1", "y\n")]

    events = _read_audit(audit)
    assert [e["event"] for e in events] == [
        "watchdog.recover.detected",
        "watchdog.recover.succeeded",
    ]
    # Both rows must carry the same recovery_id so postmortems can join.
    assert events[0]["recovery_id"] == events[1]["recovery_id"] == recovery.recovery_id


def test_tick_logs_probe_conclusion_and_summary(tmp_path: Path, caplog) -> None:
    """Logging gap: the watchdog's per-session probe decision (which prompt
    kind was classified, and why auto-answer did or didn't fire) was only
    visible via the audit JSONL for skipped model-questions -- successful
    safety-prompt probes and the tick-level summary were invisible in logs.
    Assert both the per-probe INFO line and the tick-summary INFO line."""
    caplog.set_level("INFO", logger="bernstein.core.orchestration.watchdog")
    captured: list[tuple[str, str]] = []
    snapshot = SessionSnapshot(
        session_id="s1",
        recent_output="some agent log\nContinue? [y/N]",
        is_paused=True,
        approved_prompt_classes=frozenset({"safety"}),
    )
    audit = tmp_path / "watchdog.jsonl"
    env = {FEATURE_FLAG_ENV: "1"}

    tick([snapshot], _record_calls(captured), audit, env=env)

    messages = [r.message for r in caplog.records]
    probe_lines = [m for m in messages if "watchdog probe" in m]
    assert any("session=s1" in m and "classified=safety" in m for m in probe_lines), probe_lines
    assert any("watchdog recovery SUCCEEDED" in m and "session=s1" in m for m in messages), messages
    assert any("watchdog tick complete" in m for m in messages), messages


def test_tick_skips_session_without_safety_approval(tmp_path: Path) -> None:
    captured: list[tuple[str, str]] = []
    snapshot = SessionSnapshot(
        session_id="s1",
        recent_output="Continue? [y/N]",
        is_paused=True,
        approved_prompt_classes=frozenset(),
    )
    audit = tmp_path / "watchdog.jsonl"
    env = {FEATURE_FLAG_ENV: "1"}

    result = tick([snapshot], _record_calls(captured), audit, env=env)

    assert result.recoveries == ()
    assert captured == []
    assert _read_audit(audit) == []


def test_tick_never_auto_answers_model_question(tmp_path: Path) -> None:
    captured: list[tuple[str, str]] = []
    snapshot = SessionSnapshot(
        session_id="s1",
        recent_output="Which of these two paths did you mean?",
        is_paused=True,
        approved_prompt_classes=frozenset({"safety"}),
    )
    audit = tmp_path / "watchdog.jsonl"
    env = {FEATURE_FLAG_ENV: "1"}

    result = tick([snapshot], _record_calls(captured), audit, env=env)

    assert result.recoveries == ()
    assert result.skipped_model_questions == ("s1",)
    assert captured == []
    events = _read_audit(audit)
    assert events[0]["event"] == "watchdog.recover.skipped"
    assert events[0]["rule"] == "prompt_waiting.model_question"


def test_tick_skips_running_sessions(tmp_path: Path) -> None:
    captured: list[tuple[str, str]] = []
    snapshot = SessionSnapshot(
        session_id="s1",
        recent_output="Continue? [y/N]",
        is_paused=False,
        approved_prompt_classes=frozenset({"safety"}),
    )
    audit = tmp_path / "watchdog.jsonl"
    env = {FEATURE_FLAG_ENV: "1"}

    result = tick([snapshot], _record_calls(captured), audit, env=env)

    assert result.recoveries == ()
    assert captured == []


def test_tick_records_failed_delivery(tmp_path: Path) -> None:
    snapshot = SessionSnapshot(
        session_id="s1",
        recent_output="Continue? [y/N]",
        is_paused=True,
        approved_prompt_classes=frozenset({"safety"}),
    )
    audit = tmp_path / "watchdog.jsonl"
    env = {FEATURE_FLAG_ENV: "1"}

    def _broken_respond(session_id: str, keystroke: str) -> bool:
        return False

    result = tick([snapshot], _broken_respond, audit, env=env)
    assert len(result.recoveries) == 1
    events = _read_audit(audit)
    assert [e["event"] for e in events] == [
        "watchdog.recover.detected",
        "watchdog.recover.failed",
    ]


def test_tick_isolates_respond_exceptions(tmp_path: Path) -> None:
    """An exception from respond must not bubble out of the tick."""
    snapshot = SessionSnapshot(
        session_id="s1",
        recent_output="Continue? [y/N]",
        is_paused=True,
        approved_prompt_classes=frozenset({"safety"}),
    )
    audit = tmp_path / "watchdog.jsonl"
    env = {FEATURE_FLAG_ENV: "1"}

    def _raising_respond(session_id: str, keystroke: str) -> bool:
        raise RuntimeError("simulated adapter write failure")

    # Should not raise.
    result = tick([snapshot], _raising_respond, audit, env=env)
    assert len(result.recoveries) == 1
    events = _read_audit(audit)
    assert [e["event"] for e in events] == [
        "watchdog.recover.detected",
        "watchdog.recover.failed",
    ]


def test_tick_handles_multiple_sessions(tmp_path: Path) -> None:
    """Each paused session is evaluated independently in a single tick."""
    captured: list[tuple[str, str]] = []
    audit = tmp_path / "watchdog.jsonl"
    env = {FEATURE_FLAG_ENV: "1"}
    snapshots = [
        # Eligible safety prompt - auto-answered.
        SessionSnapshot(
            session_id="s-safety",
            recent_output="Continue? [y/N]",
            is_paused=True,
            approved_prompt_classes=frozenset({"safety"}),
        ),
        # Model question - escalated, not answered.
        SessionSnapshot(
            session_id="s-question",
            recent_output="Which file did you mean?",
            is_paused=True,
            approved_prompt_classes=frozenset({"safety"}),
        ),
        # Running session - skipped entirely.
        SessionSnapshot(
            session_id="s-running",
            recent_output="Continue? [y/N]",
            is_paused=False,
            approved_prompt_classes=frozenset({"safety"}),
        ),
        # No safety approval - skipped.
        SessionSnapshot(
            session_id="s-noapproval",
            recent_output="Continue? [y/N]",
            is_paused=True,
            approved_prompt_classes=frozenset(),
        ),
    ]

    result = tick(snapshots, _record_calls(captured), audit, env=env)

    assert [r.session_id for r in result.recoveries] == ["s-safety"]
    assert result.skipped_model_questions == ("s-question",)
    assert captured == [("s-safety", "y\n")]


def test_tick_audit_recovery_id_is_unique_per_session(tmp_path: Path) -> None:
    """Two recoveries in the same tick must get distinct recovery_id values."""
    captured: list[tuple[str, str]] = []
    audit = tmp_path / "watchdog.jsonl"
    env = {FEATURE_FLAG_ENV: "1"}
    snapshots = [
        SessionSnapshot(
            session_id="s1",
            recent_output="Continue? [y/N]",
            is_paused=True,
            approved_prompt_classes=frozenset({"safety"}),
        ),
        SessionSnapshot(
            session_id="s2",
            recent_output="Proceed? [y/N]",
            is_paused=True,
            approved_prompt_classes=frozenset({"safety"}),
        ),
    ]

    result = tick(snapshots, _record_calls(captured), audit, env=env)
    assert len(result.recoveries) == 2
    ids = {r.recovery_id for r in result.recoveries}
    assert len(ids) == 2

    events = _read_audit(audit)
    # detected + succeeded per session.
    assert len(events) == 4
    # Every detected event must be paired with a succeeded event sharing
    # the same recovery_id.
    by_id: dict[str, list[str]] = {}
    for ev in events:
        by_id.setdefault(str(ev["recovery_id"]), []).append(str(ev["event"]))
    for rid, evs in by_id.items():
        assert evs == ["watchdog.recover.detected", "watchdog.recover.succeeded"], rid
