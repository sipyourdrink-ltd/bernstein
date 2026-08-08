"""Unit tests for the parked-session store's empty-vs-unavailable distinction (#3453).

Step 1 of #3453: ``load_parked_sessions()`` must be able to tell a caller
"the store was never written this run" apart from "the store was written and
is genuinely empty" - both previously returned the same bare empty ``set``.
"""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.core.orchestration.supervisor_aggregator import (
    ParkedSessions,
    SupervisorSnapshot,
    format_summary_line,
    load_parked_sessions,
    snapshot_to_dict,
)


def _write_marker(workdir: Path, session_ids: list[str]) -> None:
    marker_dir = workdir / ".sdd" / "runtime" / "spawn_supervisor"
    marker_dir.mkdir(parents=True, exist_ok=True)
    (marker_dir / "parked.json").write_text(json.dumps({"session_ids": session_ids}))


def test_no_marker_and_no_failures_is_unavailable(tmp_path: Path) -> None:
    """With no marker file and no failures dir, the store was never written."""
    result = load_parked_sessions(tmp_path)
    assert result == ParkedSessions(available=False, session_ids=frozenset())
    assert not result.available
    assert not result


def test_marker_present_and_empty_is_available_and_zero(tmp_path: Path) -> None:
    """An explicit empty marker means the supervisor ran and found nothing."""
    _write_marker(tmp_path, [])
    result = load_parked_sessions(tmp_path)
    assert result.available
    assert result.session_ids == frozenset()
    assert not result


def test_marker_present_with_entries_is_available_with_ids(tmp_path: Path) -> None:
    """A marker carrying ids is returned as-is, and membership checks work."""
    _write_marker(tmp_path, ["sess-a", "sess-b"])
    result = load_parked_sessions(tmp_path)
    assert result.available
    assert result.session_ids == frozenset({"sess-a", "sess-b"})
    assert "sess-a" in result
    assert "sess-c" not in result
    assert len(result) == 2


def test_respawn_exhausted_fallback_makes_the_result_available(tmp_path: Path) -> None:
    """A lifecycle-log entry is also evidence the supervisor ran, absent the marker."""
    failures_dir = tmp_path / ".sdd" / "runtime" / "failures"
    failures_dir.mkdir(parents=True)
    (failures_dir / "one.json").write_text(json.dumps({"kind": "respawn_exhausted", "session_id": "sess-z"}))

    result = load_parked_sessions(tmp_path)
    assert result.available
    assert result.session_ids == frozenset({"sess-z"})


def test_unreadable_marker_does_not_mark_the_result_available(tmp_path: Path) -> None:
    """A corrupt marker file is treated the same as an absent one, not a lie about availability."""
    marker_dir = tmp_path / ".sdd" / "runtime" / "spawn_supervisor"
    marker_dir.mkdir(parents=True)
    (marker_dir / "parked.json").write_text("{not valid json")

    result = load_parked_sessions(tmp_path)
    assert not result.available
    assert result.session_ids == frozenset()


def test_summary_line_distinguishes_unavailable_from_genuinely_zero() -> None:
    """The property #3453 exists to test: absent and empty must render differently."""
    unavailable = SupervisorSnapshot(
        schema_version="1.0.0",
        generated_ts=0.0,
        workers=(),
        parked_available=False,
    )
    zero_but_tracked = SupervisorSnapshot(
        schema_version="1.0.0",
        generated_ts=0.0,
        workers=(),
        parked_available=True,
    )

    unavailable_line = format_summary_line(unavailable)
    zero_line = format_summary_line(zero_but_tracked)

    assert unavailable_line != zero_line
    assert "unavailable" in unavailable_line
    assert "unavailable" not in zero_line

    assert snapshot_to_dict(unavailable)["parked_available"] is False
    assert snapshot_to_dict(zero_but_tracked)["parked_available"] is True
