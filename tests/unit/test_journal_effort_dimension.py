"""Tests for the reasoning-effort dimension in the step-hash journal.

The effort a step is routed at changes the model's output, so a replay at a
different effort must surface as step-hash divergence rather than a silent
mismatch. These tests pin the load-bearing contract:

* Effort is folded into the step hash only when recorded (non-``None``), so a
  cross-effort replay diverges.
* A ``None`` (legacy / unrecorded) effort is byte-identical to a pre-effort
  record, so every journal written before the effort dimension re-verifies
  unchanged (back-compat).
* An on-disk row that has no ``effort`` / ``schema_version`` field (exactly the
  legacy shape) still verifies, and is read as ``schema_version=1``.

Each test is written to FAIL against the pre-effort code and PASS against the
effort-aware code.
"""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.core.persistence.journal import (
    GENESIS_HASH,
    JOURNAL_SCHEMA_VERSION,
    Journal,
    JournalEntry,
    JournalReader,
    canonical_step_payload,
    compute_step_hash,
)


class TestEffortFoldedIntoHash:
    """A recorded effort changes the step hash; an absent one does not."""

    def test_none_effort_matches_pre_effort_payload(self) -> None:
        # The canonical payload with effort=None must be byte-identical to the
        # six-field payload a pre-effort verifier would produce, so old records
        # re-derive unchanged.
        expected = json.dumps(
            {
                "prev_hash": GENESIS_HASH,
                "input_hash": "aa",
                "model": "m1",
                "prompt": "p1",
                "tool_call": None,
                "tool_result": None,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        got = canonical_step_payload(
            prev_hash=GENESIS_HASH,
            input_hash="aa",
            model="m1",
            prompt="p1",
            tool_call=None,
            tool_result=None,
            effort=None,
        )
        assert got == expected

    def test_recorded_effort_changes_hash(self) -> None:
        base = compute_step_hash(
            prev_hash=GENESIS_HASH,
            input_hash="aa",
            model="m1",
            prompt="p1",
            tool_call=None,
            tool_result=None,
            effort=None,
        )
        high = compute_step_hash(
            prev_hash=GENESIS_HASH,
            input_hash="aa",
            model="m1",
            prompt="p1",
            tool_call=None,
            tool_result=None,
            effort="high",
        )
        assert high != base, "a recorded effort must change the step hash"

    def test_different_effort_produces_different_hash(self) -> None:
        low = compute_step_hash(
            prev_hash=GENESIS_HASH,
            input_hash="aa",
            model="m1",
            prompt="p1",
            tool_call=None,
            tool_result=None,
            effort="low",
        )
        maxx = compute_step_hash(
            prev_hash=GENESIS_HASH,
            input_hash="aa",
            model="m1",
            prompt="p1",
            tool_call=None,
            tool_result=None,
            effort="max",
        )
        assert low != maxx, "distinct efforts must diverge in the step hash"


class TestEffortBackCompat:
    """Legacy rows (no effort / no schema_version) still validate."""

    def test_legacy_row_without_effort_verifies(self, tmp_path: Path) -> None:
        # Hand-write a row in the exact pre-effort shape: no ``effort`` key,
        # no ``schema_version`` key, hash computed over the six-field payload.
        step_hash = compute_step_hash(
            prev_hash=GENESIS_HASH,
            input_hash="aa",
            model="m1",
            prompt="p1",
            tool_call=None,
            tool_result=None,
        )
        legacy_row = {
            "seq": 0,
            "prev_hash": GENESIS_HASH,
            "input_hash": "aa",
            "model": "m1",
            "prompt": "p1",
            "tool_call": None,
            "tool_result": None,
            "step_hash": step_hash,
            "ts": 0.0,
            "blob_refs": [],
        }
        bucket = tmp_path / "000000.jsonl"
        bucket.write_text(
            json.dumps(legacy_row, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

        result = JournalReader(tmp_path).verify()
        assert result.ok, f"legacy row must verify; errors={result.errors}"
        assert result.steps == 1

        # And it re-opens (recovery revalidates the chain) without raising.
        journal = Journal.open(tmp_path)
        assert journal.head_hash == step_hash
        assert journal.next_seq == 1
        journal.close()

    def test_legacy_row_reads_as_schema_version_one(self) -> None:
        legacy_row = {
            "seq": 0,
            "prev_hash": GENESIS_HASH,
            "input_hash": "aa",
            "model": "m1",
            "prompt": "p1",
            "tool_call": None,
            "tool_result": None,
            "step_hash": "x",
            "ts": 0.0,
        }
        entry = JournalEntry.from_dict(legacy_row)
        assert entry.effort is None
        assert entry.schema_version == 1


class TestEffortRoundTrip:
    """Appending with an effort persists and re-verifies it."""

    def test_append_with_effort_roundtrips_and_verifies(self, tmp_path: Path) -> None:
        journal = Journal.open(tmp_path)
        entry = journal.append(input_hash="aa", model="m1", prompt="p1", effort="high")
        journal.close()

        assert entry.effort == "high"
        assert entry.schema_version == JOURNAL_SCHEMA_VERSION

        reader = JournalReader(tmp_path)
        head = reader.head()
        assert head is not None
        assert head.effort == "high"
        assert reader.verify().ok

    def test_effort_change_breaks_verification(self, tmp_path: Path) -> None:
        # Persist a step at effort=high, then rewrite the on-disk effort to
        # "low" without recomputing the hash. Verification must reject it: a
        # step replayed at a different effort is divergence, not noise.
        journal = Journal.open(tmp_path)
        journal.append(input_hash="aa", model="m1", prompt="p1", effort="high")
        journal.close()

        bucket = tmp_path / "000000.jsonl"
        rows = [json.loads(line) for line in bucket.read_text().splitlines() if line.strip()]
        rows[0]["effort"] = "low"  # tamper: effort no longer matches step_hash
        bucket.write_text(
            "\n".join(json.dumps(r, sort_keys=True, separators=(",", ":")) for r in rows) + "\n",
            encoding="utf-8",
        )

        result = JournalReader(tmp_path).verify()
        assert not result.ok
        assert any("step_hash mismatch" in e for e in result.errors)
