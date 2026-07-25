"""Production events are a pure, replayable projection of the spine (#2559).

Covers the substrate the Phase-4 acceptance criteria stand on:

* the event is a function of the spine entry alone -- nothing ambient leaks in,
  so replaying reproduces byte-identical events;
* a single-byte mutation of any spine row flips the *replayed* event to
  ``verified: false`` while the journaled row still claims ``true``, and the
  disagreement is reported as a named divergence;
* the emit path never raises, whatever the filesystem or a subscriber does.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.lineage.artifact_events import (
    ARTIFACT_EVENTS_FILENAME,
    DIVERGENCE_ALTERED,
    DIVERGENCE_DROPPED,
    DIVERGENCE_DUPLICATED,
    DIVERGENCE_UNEXPECTED,
    ArtifactProductionEvent,
    append_production_event,
    compare_fanout,
    emit_production_event,
    load_production_events,
    observed_artifact_keys,
    project_production_event,
    replay_production_events,
)
from bernstein.core.lineage.spine import JOURNAL_SEAL_STEP_PREFIX, LineageSpine, SpineEntry

_KEY = b"k" * 32
_RUN = "run-events"


def _spine(tmp_path: Path, run_id: str = _RUN) -> LineageSpine:
    return LineageSpine(tmp_path / ".sdd" / "lineage", run_id=run_id, hmac_key=_KEY)


def _root(tmp_path: Path) -> Path:
    return tmp_path / ".sdd" / "lineage"


def _record(spine: LineageSpine, path: str, content: bytes, *, ts: int, step_id: str = "step") -> SpineEntry:
    return spine.record_entry(
        artifact_path=path,
        content=content,
        actor="agent-a",
        step_id=step_id,
        model="model-x",
        timestamp=ts,
    )


# ---------------------------------------------------------------------------
# The projection is pure
# ---------------------------------------------------------------------------


def test_event_is_a_function_of_the_entry_alone(tmp_path: Path) -> None:
    """Two projections of one entry are byte-identical."""
    entry = _record(_spine(tmp_path), "docs/report.md", b"one", ts=10)
    a = project_production_event(entry, run_id=_RUN, verified=True)
    b = project_production_event(entry, run_id=_RUN, verified=True)
    assert a == b
    assert a.canonical_bytes() == b.canonical_bytes()


def test_projection_copies_every_provenance_field(tmp_path: Path) -> None:
    entry = _record(_spine(tmp_path), "pkg://pypi/bernstein/3.9.0", b"wheel", ts=42, step_id="s-7")
    event = project_production_event(entry, run_id=_RUN, verified=True)
    assert event.uri == "pkg://pypi/bernstein/3.9.0"
    assert event.entry_hash == entry.entry_hash
    assert event.content_hash == entry.content_hash
    assert event.actor == "agent-a"
    assert event.model == "model-x"
    assert event.step_id == "s-7"
    assert event.run_id == _RUN
    assert event.timestamp == 42


def test_payload_round_trips_through_the_journal_form(tmp_path: Path) -> None:
    entry = _record(_spine(tmp_path), "a.txt", b"x", ts=1)
    event = project_production_event(entry, run_id=_RUN, verified=True)
    assert ArtifactProductionEvent.from_payload(json.loads(event.canonical_bytes())) == event


def test_unknown_wire_version_is_refused_not_guessed(tmp_path: Path) -> None:
    entry = _record(_spine(tmp_path), "a.txt", b"x", ts=1)
    row = json.loads(project_production_event(entry, run_id=_RUN, verified=True).canonical_bytes())
    row["v"] = 99
    with pytest.raises(ValueError, match="unsupported artifact event version"):
        ArtifactProductionEvent.from_payload(row)


# ---------------------------------------------------------------------------
# Journal IO
# ---------------------------------------------------------------------------


def test_journal_preserves_append_order(tmp_path: Path) -> None:
    spine = _spine(tmp_path)
    for i, name in enumerate(["a.txt", "b.txt", "c.txt"]):
        entry = _record(spine, name, f"v{i}".encode(), ts=i)
        emit_production_event(_root(tmp_path), run_id=_RUN, entry=entry)
    assert [e.uri for e in load_production_events(_root(tmp_path), run_id=_RUN)] == ["a.txt", "b.txt", "c.txt"]


def test_journal_lives_beside_the_spine(tmp_path: Path) -> None:
    entry = _record(_spine(tmp_path), "a.txt", b"x", ts=1)
    emit_production_event(_root(tmp_path), run_id=_RUN, entry=entry)
    assert (_root(tmp_path) / _RUN / ARTIFACT_EVENTS_FILENAME).is_file()
    assert (_root(tmp_path) / _RUN / "spine.jsonl").is_file()


def test_malformed_journal_row_is_skipped_not_fatal(tmp_path: Path) -> None:
    entry = _record(_spine(tmp_path), "a.txt", b"x", ts=1)
    emit_production_event(_root(tmp_path), run_id=_RUN, entry=entry)
    path = _root(tmp_path) / _RUN / ARTIFACT_EVENTS_FILENAME
    path.write_bytes(b"{not json}\n" + path.read_bytes())
    # The good row survives: a corrupt index must never stop a verifier from
    # reaching the spine, which is the thing that actually decides.
    assert [e.uri for e in load_production_events(_root(tmp_path), run_id=_RUN)] == ["a.txt"]


def test_missing_journal_reads_as_empty(tmp_path: Path) -> None:
    assert load_production_events(_root(tmp_path), run_id="never-ran") == []


def test_run_id_escaping_its_directory_is_refused(tmp_path: Path) -> None:
    entry = _record(_spine(tmp_path), "a.txt", b"x", ts=1)
    with pytest.raises(ValueError, match="path separator"):
        append_production_event(
            _root(tmp_path),
            run_id="../escape",
            event=project_production_event(entry, run_id="x", verified=True),
        )


# ---------------------------------------------------------------------------
# Fail-open (AC7)
# ---------------------------------------------------------------------------


def test_emit_never_raises_when_the_journal_cannot_be_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    entry = _record(_spine(tmp_path), "a.txt", b"x", ts=1)

    def _boom(*_a: object, **_k: object) -> None:
        raise OSError("no space left on device")

    monkeypatch.setattr("bernstein.core.lineage.artifact_events.append_production_event", _boom)
    # Returns the event regardless: the spine entry is already durable and the
    # event set is re-derivable, so a journal failure loses latency, not facts.
    assert emit_production_event(_root(tmp_path), run_id=_RUN, entry=entry) is not None


def test_emit_never_raises_when_a_subscriber_explodes(tmp_path: Path) -> None:
    entry = _record(_spine(tmp_path), "a.txt", b"x", ts=1)

    def _bad_subscriber(_event: object) -> None:
        raise RuntimeError("subscriber is on fire")

    event = emit_production_event(_root(tmp_path), run_id=_RUN, entry=entry, publish=_bad_subscriber)
    assert event is not None
    # The journal still got the row: publishing and journaling fail apart.
    assert len(load_production_events(_root(tmp_path), run_id=_RUN)) == 1


# ---------------------------------------------------------------------------
# Replay and tamper (AC2)
# ---------------------------------------------------------------------------


def test_replay_reproduces_the_journal_exactly(tmp_path: Path) -> None:
    spine = _spine(tmp_path)
    for i in range(4):
        entry = _record(spine, f"f{i}.txt", f"c{i}".encode(), ts=i)
        emit_production_event(_root(tmp_path), run_id=_RUN, entry=entry)

    journaled = load_production_events(_root(tmp_path), run_id=_RUN)
    replayed = replay_production_events(_root(tmp_path), run_id=_RUN, hmac_key=_KEY)
    assert [e.canonical_bytes() for e in journaled] == [e.canonical_bytes() for e in replayed]
    assert compare_fanout(journaled, replayed) == []


@pytest.mark.parametrize("field", ["content_hash", "actor", "model", "artifact_path", "hmac"])
def test_single_byte_flip_replays_as_unverified(tmp_path: Path, field: str) -> None:
    """AC2: flip one byte of a spine row and the event replays ``verified: false``."""
    spine = _spine(tmp_path)
    entry = _record(spine, "docs/report.md", b"payload", ts=7)
    emit_production_event(_root(tmp_path), run_id=_RUN, entry=entry)

    spine_path = _root(tmp_path) / _RUN / "spine.jsonl"
    row = json.loads(spine_path.read_bytes().strip())
    row[field] = row[field][:-1] + ("0" if row[field][-1] != "0" else "1")
    spine_path.write_bytes(json.dumps(row, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode() + b"\n")

    replayed = replay_production_events(_root(tmp_path), run_id=_RUN, hmac_key=_KEY)
    assert [e.verified for e in replayed] == [False]


def test_tamper_surfaces_as_an_altered_divergence_naming_the_entry(tmp_path: Path) -> None:
    spine = _spine(tmp_path)
    entry = _record(spine, "docs/report.md", b"payload", ts=7)
    emit_production_event(_root(tmp_path), run_id=_RUN, entry=entry)

    spine_path = _root(tmp_path) / _RUN / "spine.jsonl"
    row = json.loads(spine_path.read_bytes().strip())
    row["actor"] = "someone-else"
    spine_path.write_bytes(json.dumps(row, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode() + b"\n")

    divergences = compare_fanout(
        load_production_events(_root(tmp_path), run_id=_RUN),
        replay_production_events(_root(tmp_path), run_id=_RUN, hmac_key=_KEY),
    )
    assert [d.kind for d in divergences] == [DIVERGENCE_ALTERED]
    assert divergences[0].entry_hash == entry.entry_hash


def test_a_wrong_hmac_key_marks_every_event_unverified(tmp_path: Path) -> None:
    spine = _spine(tmp_path)
    _record(spine, "a.txt", b"x", ts=1)
    replayed = replay_production_events(_root(tmp_path), run_id=_RUN, hmac_key=b"a-different-key")
    assert [e.verified for e in replayed] == [False]


# ---------------------------------------------------------------------------
# Divergence kinds (AC3)
# ---------------------------------------------------------------------------


def test_dropped_firing_is_reported_and_names_the_entry(tmp_path: Path) -> None:
    spine = _spine(tmp_path)
    kept = _record(spine, "a.txt", b"a", ts=1)
    dropped = _record(spine, "b.txt", b"b", ts=2)
    emit_production_event(_root(tmp_path), run_id=_RUN, entry=kept)
    # `dropped` never reaches the journal, as if the emit had been lost.

    divergences = compare_fanout(
        load_production_events(_root(tmp_path), run_id=_RUN),
        replay_production_events(_root(tmp_path), run_id=_RUN, hmac_key=_KEY),
    )
    assert [(d.kind, d.entry_hash) for d in divergences] == [(DIVERGENCE_DROPPED, dropped.entry_hash)]


def test_duplicated_firing_is_reported_and_names_the_entry(tmp_path: Path) -> None:
    spine = _spine(tmp_path)
    entry = _record(spine, "a.txt", b"a", ts=1)
    emit_production_event(_root(tmp_path), run_id=_RUN, entry=entry)
    emit_production_event(_root(tmp_path), run_id=_RUN, entry=entry)

    divergences = compare_fanout(
        load_production_events(_root(tmp_path), run_id=_RUN),
        replay_production_events(_root(tmp_path), run_id=_RUN, hmac_key=_KEY),
    )
    assert [(d.kind, d.entry_hash) for d in divergences] == [(DIVERGENCE_DUPLICATED, entry.entry_hash)]


def test_journaled_event_with_no_spine_entry_is_unexpected(tmp_path: Path) -> None:
    spine = _spine(tmp_path)
    entry = _record(spine, "a.txt", b"a", ts=1)
    forged = project_production_event(entry, run_id=_RUN, verified=True).__class__(
        uri="pkg://pypi/ghost/1.0",
        entry_hash="sha256:" + "f" * 64,
        content_hash="sha256:" + "e" * 64,
        actor="nobody",
        model="",
        step_id="",
        run_id=_RUN,
        timestamp=99,
        verified=True,
    )
    emit_production_event(_root(tmp_path), run_id=_RUN, entry=entry)
    append_production_event(_root(tmp_path), run_id=_RUN, event=forged)

    divergences = compare_fanout(
        load_production_events(_root(tmp_path), run_id=_RUN),
        replay_production_events(_root(tmp_path), run_id=_RUN, hmac_key=_KEY),
    )
    assert [(d.kind, d.entry_hash) for d in divergences] == [(DIVERGENCE_UNEXPECTED, forged.entry_hash)]


def test_divergence_order_is_deterministic(tmp_path: Path) -> None:
    spine = _spine(tmp_path)
    entries = [_record(spine, f"f{i}.txt", f"c{i}".encode(), ts=i) for i in range(5)]
    emit_production_event(_root(tmp_path), run_id=_RUN, entry=entries[0])

    journaled = load_production_events(_root(tmp_path), run_id=_RUN)
    replayed = replay_production_events(_root(tmp_path), run_id=_RUN, hmac_key=_KEY)
    first = [d.to_dict() for d in compare_fanout(journaled, replayed)]
    second = [d.to_dict() for d in compare_fanout(journaled, replayed)]
    assert first == second
    assert first == sorted(first, key=lambda d: (d["kind"], d["entry_hash"]))


# ---------------------------------------------------------------------------
# Observation
# ---------------------------------------------------------------------------


def test_observed_keys_exclude_the_runs_own_journal_seal(tmp_path: Path) -> None:
    spine = _spine(tmp_path)
    deliverable = _record(spine, "dist/pkg.whl", b"wheel", ts=1)
    seal = _record(
        spine,
        f".sdd/runs/{_RUN}/journal.jsonl",
        b"journal",
        ts=2,
        step_id=f"{JOURNAL_SEAL_STEP_PREFIX}sha256:abc",
    )
    for entry in (deliverable, seal):
        emit_production_event(_root(tmp_path), run_id=_RUN, entry=entry)

    # Both are journaled -- the boundary has no exceptions (AC8) ...
    assert len(load_production_events(_root(tmp_path), run_id=_RUN)) == 2
    # ... but the run recording itself is not something the run produced.
    assert observed_artifact_keys(_root(tmp_path), run_id=_RUN) == ("dist/pkg.whl",)


def test_observed_keys_are_sorted_and_deduplicated(tmp_path: Path) -> None:
    spine = _spine(tmp_path)
    for i, name in enumerate(["z.txt", "a.txt", "z.txt"]):
        entry = _record(spine, name, f"c{i}".encode(), ts=i)
        emit_production_event(_root(tmp_path), run_id=_RUN, entry=entry)
    assert observed_artifact_keys(_root(tmp_path), run_id=_RUN) == ("a.txt", "z.txt")


def test_no_production_is_an_empty_tuple_not_an_error(tmp_path: Path) -> None:
    """An empty observation is a genuine observation, distinct from ``None``."""
    assert observed_artifact_keys(_root(tmp_path), run_id="quiet-run") == ()
