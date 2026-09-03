"""The trajectory exporter re-emits the committed step hash, never mints one (#2926).

A trajectory row is only usable as governed learning data if the hash it
carries is the hash the run committed. If the projection recomputed the
digest, a row whose ``prompt`` had been edited after the fact would still
carry a self-consistent hash and pass any downstream check - the export
would attest to itself rather than to the run.

These tests pin that property at the row layer:

* every projected row's ``step_hash`` is byte-identical to
  ``compute_step_hash()`` over the same journal inputs;
* an edited row is caught, because the projection copies the stored hash
  instead of re-deriving one that would agree with the edit;
* projecting the same journal twice yields byte-identical canonical rows;
* a legacy ``effort=None`` row keeps the sentinel-by-omission shape, so it
  canonicalises exactly as it did before the effort dimension existed.
"""

from __future__ import annotations

import json
from itertools import pairwise

from bernstein.core.persistence.journal import (
    GENESIS_HASH,
    Journal,
    JournalEntry,
    JournalReader,
    compute_step_hash,
)
from bernstein.core.replay.trajectory import (
    TRAJECTORY_SCHEMA_VERSION,
    TrajectoryStep,
    project_trajectory,
    trajectory_step_from_entry,
)


def _seed_journal(agent_dir):
    """Write a three-step chain: two effort-bearing rows and one legacy row."""
    with Journal.open(agent_dir) as journal:
        journal.append(
            input_hash="a" * 64,
            model="claude-3-7-sonnet-20250219",
            prompt="list the failing tests",
            tool_call={"name": "bash", "args": {"cmd": "pytest -q"}},
            tool_result={"exit_code": 1, "stdout": "1 failed"},
            effort="high",
        )
        journal.append(
            input_hash="b" * 64,
            model="claude-3-7-sonnet-20250219",
            prompt="fix the assertion",
            tool_call={"name": "edit", "args": {"path": "x.py"}},
            tool_result={"ok": True},
            effort="low",
        )
        # A legacy row: no recorded effort, so the effort key is omitted
        # from both the hashed document and the projected row.
        journal.append(
            input_hash="c" * 64,
            model=None,
            prompt=None,
            tool_call=None,
            tool_result=None,
        )
    return JournalReader(agent_dir)


def test_projected_step_hash_equals_the_committed_journal_hash(tmp_path) -> None:
    """Every row carries the hash the journal wrote, re-derivable by hand."""
    reader = _seed_journal(tmp_path / "agent-1")
    entries = list(reader.entries())
    rows = project_trajectory(reader)

    assert len(rows) == len(entries) == 3
    for entry, row in zip(entries, rows, strict=True):
        expected = compute_step_hash(
            prev_hash=entry.prev_hash,
            input_hash=entry.input_hash,
            model=entry.model,
            prompt=entry.prompt,
            tool_call=entry.tool_call,
            tool_result=entry.tool_result,
            effort=entry.effort,
        )
        assert row.step_hash == entry.step_hash == expected


def test_row_chain_links_through_prev_step_hash(tmp_path) -> None:
    """The rows carry the chain, so a consumer can walk it without the journal."""
    reader = _seed_journal(tmp_path / "agent-1")
    rows = project_trajectory(reader)

    assert rows[0].prev_step_hash == GENESIS_HASH
    assert [row.index for row in rows] == [0, 1, 2]
    for previous, current in pairwise(rows):
        assert current.prev_step_hash == previous.step_hash


def test_projection_re_emits_and_never_recomputes_an_edited_row() -> None:
    """A tampered field does not get a fresh hash minted for it.

    This is the load-bearing property. The entry below claims a hash for one
    prompt while carrying another; the projection must surface the claimed
    hash unchanged so the divergence stays detectable downstream.
    """
    committed_hash = compute_step_hash(
        prev_hash=GENESIS_HASH,
        input_hash="a" * 64,
        model="m",
        prompt="the prompt the run actually sent",
        tool_call=None,
        tool_result=None,
        effort="high",
    )
    tampered = JournalEntry(
        seq=0,
        prev_hash=GENESIS_HASH,
        input_hash="a" * 64,
        model="m",
        prompt="a prompt substituted after the run",
        tool_call=None,
        tool_result=None,
        step_hash=committed_hash,
        ts=0.0,
        effort="high",
    )

    row = trajectory_step_from_entry(tampered)

    assert row.step_hash == committed_hash
    recomputed = compute_step_hash(
        prev_hash=row.prev_step_hash,
        input_hash=row.input_hash,
        model=row.model,
        prompt=row.observation,
        tool_call=row.action,
        tool_result=row.outcome,
        effort=row.effort,
    )
    assert row.step_hash != recomputed


def test_projecting_the_same_journal_twice_is_byte_identical(tmp_path) -> None:
    """Determinism at the row layer: same journal in, same bytes out."""
    reader = _seed_journal(tmp_path / "agent-1")

    first = [row.canonical_bytes() for row in project_trajectory(reader)]
    second = [row.canonical_bytes() for row in project_trajectory(reader)]

    assert first == second
    # And the bytes carry no wall-clock: ``ts`` never enters the document.
    for blob in first:
        assert "ts" not in json.loads(blob.decode("utf-8"))


def test_legacy_row_omits_effort_from_its_canonical_bytes(tmp_path) -> None:
    """Sentinel-by-omission: an unrecorded effort leaves no key behind.

    The journal hashes a legacy row without an ``effort`` key so it stays
    byte-identical to a pre-effort record. The trajectory row mirrors that
    contract, so adding the effort dimension did not change the bytes of any
    row that predates it.
    """
    reader = _seed_journal(tmp_path / "agent-1")
    rows = project_trajectory(reader)

    legacy = rows[2]
    assert legacy.effort is None
    assert "effort" not in json.loads(legacy.canonical_bytes().decode("utf-8"))

    recorded = rows[0]
    assert recorded.effort == "high"
    assert json.loads(recorded.canonical_bytes().decode("utf-8"))["effort"] == "high"


def test_row_projects_observation_action_and_outcome(tmp_path) -> None:
    """The consumer-facing column names map onto the journal fields."""
    reader = _seed_journal(tmp_path / "agent-1")
    row = project_trajectory(reader)[0]

    assert row.observation == "list the failing tests"
    assert row.input_hash == "a" * 64
    assert row.action == {"name": "bash", "args": {"cmd": "pytest -q"}}
    assert row.outcome == {"exit_code": 1, "stdout": "1 failed"}
    assert row.model == "claude-3-7-sonnet-20250219"
    assert row.effort == "high"
    assert row.schema_version == TRAJECTORY_SCHEMA_VERSION


def test_row_round_trips_through_dict(tmp_path) -> None:
    """The dict form is the wire shape a consumer reads; it must round-trip."""
    reader = _seed_journal(tmp_path / "agent-1")
    for row in project_trajectory(reader):
        assert TrajectoryStep.from_dict(row.to_dict()) == row


def test_empty_journal_projects_no_rows(tmp_path) -> None:
    """A run that never stepped exports an empty trajectory, not an error."""
    assert project_trajectory(JournalReader(tmp_path / "never-ran")) == []
