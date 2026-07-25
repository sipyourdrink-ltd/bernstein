"""Replayable fan-out: the fire set is a function of the spine (#2559, AC3).

Replaying a fixture spine has to reproduce the exact intended trigger fire set
recorded in the journal, and a synthetically dropped or duplicated firing has to
be reported as a divergence naming the offending ``entry_hash``. That is only
possible because :func:`intended_fires` is pure: it reads spine entries and
patterns, nothing else. No clock, no subscriber state, no "was the orchestrator
listening at the time".

Also pinned here: an artifact production normalises into the same
:class:`TriggerEvent` shape every other source emits, so artifact rules go
through the existing rule matching with no second matching engine.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.lineage.artifact_events import (
    DIVERGENCE_ALTERED,
    DIVERGENCE_DROPPED,
    DIVERGENCE_DUPLICATED,
    emit_production_event,
)
from bernstein.core.lineage.spine import JOURNAL_SEAL_STEP_PREFIX, LineageSpine, SpineEntry
from bernstein.core.trigger_sources.artifact import (
    ARTIFACT_TRIGGER_SOURCE,
    ArtifactSource,
    fire_divergences,
    intended_fires,
    matches_any,
    normalize_production_event,
)

_KEY = b"f" * 32
_RUN = "run-fanout"


def _root(tmp_path: Path) -> Path:
    return tmp_path / ".sdd" / "lineage"


def _spine(tmp_path: Path) -> LineageSpine:
    return LineageSpine(_root(tmp_path), run_id=_RUN, hmac_key=_KEY)


def _record(spine: LineageSpine, uri: str, content: bytes, *, ts: int, step_id: str = "publish") -> SpineEntry:
    return spine.record_entry(
        artifact_path=uri,
        content=content,
        actor="agent-release",
        step_id=step_id,
        model="claude-opus-5",
        timestamp=ts,
    )


def _fixture_spine(tmp_path: Path) -> list[SpineEntry]:
    """A small run: two packages, a PR, a doc, and the run's own journal seal."""
    spine = _spine(tmp_path)
    entries = [
        _record(spine, "pkg://pypi/bernstein/3.9.0", b"wheel-a", ts=1),
        _record(spine, "pr://github.com/acme/widget/2559", b"head-1", ts=2),
        _record(spine, "pkg://pypi/bernstein/3.9.1", b"wheel-b", ts=3),
        _record(spine, "doc://example.test/lineage", b"page", ts=4),
        _record(
            spine,
            f".sdd/runs/{_RUN}/journal.jsonl",
            b"journal",
            ts=5,
            step_id=f"{JOURNAL_SEAL_STEP_PREFIX}sha256:abc",
        ),
    ]
    for entry in entries:
        emit_production_event(_root(tmp_path), run_id=_RUN, entry=entry)
    return entries


# ---------------------------------------------------------------------------
# The fire set is a pure projection
# ---------------------------------------------------------------------------


def test_replaying_the_spine_reproduces_the_identical_fire_set(tmp_path: Path) -> None:
    entries = _fixture_spine(tmp_path)
    first = intended_fires(entries, run_id=_RUN, hmac_key=_KEY)
    second = intended_fires(entries, run_id=_RUN, hmac_key=_KEY)
    assert [e.canonical_bytes() for e in first] == [e.canonical_bytes() for e in second]


def test_the_runs_own_journal_seal_never_fires(tmp_path: Path) -> None:
    """The run recording itself is not an output anything should react to."""
    entries = _fixture_spine(tmp_path)
    fires = intended_fires(entries, run_id=_RUN, hmac_key=_KEY)
    assert all(not e.is_journal_seal for e in fires)
    assert len(fires) == len(entries) - 1


def test_patterns_narrow_the_fire_set(tmp_path: Path) -> None:
    entries = _fixture_spine(tmp_path)
    fires = intended_fires(entries, run_id=_RUN, hmac_key=_KEY, patterns=["pkg://pypi/bernstein/*"])
    assert [e.uri for e in fires] == ["pkg://pypi/bernstein/3.9.0", "pkg://pypi/bernstein/3.9.1"]


def test_an_empty_pattern_list_fires_on_everything_produced(tmp_path: Path) -> None:
    entries = _fixture_spine(tmp_path)
    assert len(intended_fires(entries, run_id=_RUN, hmac_key=_KEY, patterns=[])) == 4


def test_a_malformed_pattern_matches_nothing_instead_of_raising(tmp_path: Path) -> None:
    entries = _fixture_spine(tmp_path)
    fires = intended_fires(entries, run_id=_RUN, hmac_key=_KEY, patterns=["ftp://evil.test/*", "../etc/passwd"])
    assert fires == []


def test_fire_order_follows_append_order(tmp_path: Path) -> None:
    entries = _fixture_spine(tmp_path)
    fires = intended_fires(entries, run_id=_RUN, hmac_key=_KEY)
    assert [e.timestamp for e in fires] == [1, 2, 3, 4]


def test_matches_any_uses_the_canonical_matcher() -> None:
    assert matches_any(["pkg://pypi/bernstein/*"], "pkg://pypi/bernstein/3.9.0")
    assert not matches_any(["pkg://pypi/other/*"], "pkg://pypi/bernstein/3.9.0")
    assert matches_any(["docs/**/*.md"], "docs/nested/page.md")
    assert not matches_any(["not a key ://"], "pkg://pypi/bernstein/3.9.0")


# ---------------------------------------------------------------------------
# A tampered record cannot cause work
# ---------------------------------------------------------------------------


def test_an_unverified_entry_does_not_fire_by_default(tmp_path: Path) -> None:
    entries = _fixture_spine(tmp_path)
    # Replay under the wrong key: every entry fails per-entry verification.
    assert intended_fires(entries, run_id=_RUN, hmac_key=b"wrong-key") == []


def test_unverified_entries_can_be_inspected_when_explicitly_asked_for(tmp_path: Path) -> None:
    entries = _fixture_spine(tmp_path)
    fires = intended_fires(entries, run_id=_RUN, hmac_key=b"wrong-key", require_verified=False)
    assert len(fires) == 4
    assert all(not e.verified for e in fires)


# ---------------------------------------------------------------------------
# Divergence naming (AC3)
# ---------------------------------------------------------------------------


def test_an_intact_run_reports_no_divergence(tmp_path: Path) -> None:
    _fixture_spine(tmp_path)
    assert fire_divergences(_root(tmp_path), run_id=_RUN, hmac_key=_KEY) == []


def test_a_dropped_firing_names_the_offending_entry_hash(tmp_path: Path) -> None:
    entries = _fixture_spine(tmp_path)
    events_path = _root(tmp_path) / _RUN / "artifact-events.jsonl"
    rows = events_path.read_bytes().strip().split(b"\n")
    # Synthetically drop the third firing, as if the emit had been lost.
    events_path.write_bytes(b"\n".join(rows[:2] + rows[3:]) + b"\n")

    divergences = fire_divergences(_root(tmp_path), run_id=_RUN, hmac_key=_KEY)
    assert [(d.kind, d.entry_hash) for d in divergences] == [(DIVERGENCE_DROPPED, entries[2].entry_hash)]
    assert "pkg://pypi/bernstein/3.9.1" in divergences[0].detail


def test_a_duplicated_firing_names_the_offending_entry_hash(tmp_path: Path) -> None:
    entries = _fixture_spine(tmp_path)
    events_path = _root(tmp_path) / _RUN / "artifact-events.jsonl"
    rows = events_path.read_bytes().strip().split(b"\n")
    events_path.write_bytes(b"\n".join([*rows, rows[1]]) + b"\n")

    divergences = fire_divergences(_root(tmp_path), run_id=_RUN, hmac_key=_KEY)
    assert [(d.kind, d.entry_hash) for d in divergences] == [(DIVERGENCE_DUPLICATED, entries[1].entry_hash)]


def test_a_tampered_spine_row_diverges_from_its_journaled_firing(tmp_path: Path) -> None:
    entries = _fixture_spine(tmp_path)
    spine_path = _root(tmp_path) / _RUN / "spine.jsonl"
    rows = [json.loads(line) for line in spine_path.read_bytes().strip().split(b"\n")]
    rows[0]["actor"] = "impostor"
    spine_path.write_bytes(
        b"".join(
            json.dumps(r, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode() + b"\n" for r in rows
        )
    )

    divergences = fire_divergences(_root(tmp_path), run_id=_RUN, hmac_key=_KEY)
    assert (DIVERGENCE_ALTERED, entries[0].entry_hash) in [(d.kind, d.entry_hash) for d in divergences]


def test_divergences_are_reported_in_a_stable_order(tmp_path: Path) -> None:
    _fixture_spine(tmp_path)
    events_path = _root(tmp_path) / _RUN / "artifact-events.jsonl"
    rows = events_path.read_bytes().strip().split(b"\n")
    events_path.write_bytes(b"\n".join([rows[0], rows[0], rows[2]]) + b"\n")

    first = [d.to_dict() for d in fire_divergences(_root(tmp_path), run_id=_RUN, hmac_key=_KEY)]
    second = [d.to_dict() for d in fire_divergences(_root(tmp_path), run_id=_RUN, hmac_key=_KEY)]
    assert first == second
    assert first
    assert first == sorted(first, key=lambda d: (d["kind"], d["entry_hash"]))


# ---------------------------------------------------------------------------
# Normalisation into the existing trigger surface
# ---------------------------------------------------------------------------


def test_a_production_normalises_into_a_trigger_event(tmp_path: Path) -> None:
    entries = _fixture_spine(tmp_path)
    fires = intended_fires(entries, run_id=_RUN, hmac_key=_KEY)
    event = normalize_production_event(fires[0])

    assert event.source == ARTIFACT_TRIGGER_SOURCE
    # The key rides in changed_files so path-shaped rule filters see it.
    assert event.changed_files == ("pkg://pypi/bernstein/3.9.0",)
    assert event.sender == "agent-release"
    assert event.metadata["entry_hash"] == entries[0].entry_hash
    assert event.metadata["verified"] is True
    assert event.raw_payload["model"] == "claude-opus-5"


def test_the_source_adapter_normalises_a_raw_payload(tmp_path: Path) -> None:
    entries = _fixture_spine(tmp_path)
    payload = intended_fires(entries, run_id=_RUN, hmac_key=_KEY)[0].to_payload()
    event = ArtifactSource().normalize(payload)
    assert event.source == ARTIFACT_TRIGGER_SOURCE
    assert event.changed_files == ("pkg://pypi/bernstein/3.9.0",)


def test_a_malformed_payload_is_refused_rather_than_fired_on(tmp_path: Path) -> None:
    with pytest.raises((KeyError, ValueError, TypeError)):
        ArtifactSource().normalize({"uri": "pkg://pypi/x/1.0"})


def test_normalisation_is_deterministic(tmp_path: Path) -> None:
    entries = _fixture_spine(tmp_path)
    fire = intended_fires(entries, run_id=_RUN, hmac_key=_KEY)[0]
    a = normalize_production_event(fire)
    b = normalize_production_event(fire)
    assert (a.source, a.changed_files, a.metadata, a.raw_payload) == (
        b.source,
        b.changed_files,
        b.metadata,
        b.raw_payload,
    )
