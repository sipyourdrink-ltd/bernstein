"""Tests for the deterministic replay-debugging projection (#2605).

The debug surface is *forensic*: it freezes the recorded chain and proves
where a single run was tampered or where two runs diverged. It never
re-executes anything. These tests pin the empirical contract:

* single-run recompute-mismatch localization (streaming, first divergent
  step, named field);
* two-run divergence localization driven by ``diff_journals``;
* byte-identical, content-addressed two-run path diff across repeated
  invocations and across journal directory locations.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

from bernstein.core.persistence.journal import (
    Journal,
    JournalReader,
    compute_step_hash,
)
from bernstein.core.replay.debug import (
    HashMismatch,
    PathDiff,
    two_run_path_diff,
    walk_and_verify,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _populate(agent_dir: Path, n: int = 4, *, model: str = "m1") -> list[str]:
    """Write an ``n``-step journal and return the per-step head hashes."""
    journal = Journal.open(agent_dir)
    heads: list[str] = []
    for i in range(n):
        entry = journal.append(
            input_hash=f"a{i}",
            model=model,
            prompt=f"step {i}",
            tool_call={"name": "noop", "args": {"i": i}},
            tool_result={"ok": True, "n": i},
        )
        heads.append(entry.step_hash)
    journal.close()
    return heads


def _tamper_stored_step_hash(agent_dir: Path, seq: int) -> None:
    """Rewrite the stored ``step_hash`` of row *seq* to a bogus value.

    Leaves every canonical field intact, so the row's fields still hash to
    the original value: the divergence is a bare digest tamper.
    """
    bucket = agent_dir / "000000.jsonl"
    lines = bucket.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[seq])
    row["step_hash"] = "f" * 64
    lines[seq] = json.dumps(row, sort_keys=True, separators=(",", ":"))
    bucket.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _tamper_field_keep_prev(agent_dir: Path, seq: int) -> None:
    """Rewrite the ``prev_hash`` of row *seq* to break chain linkage."""
    bucket = agent_dir / "000000.jsonl"
    lines = bucket.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[seq])
    row["prev_hash"] = "e" * 64
    lines[seq] = json.dumps(row, sort_keys=True, separators=(",", ":"))
    bucket.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# walk_and_verify - single-run recompute-mismatch localization
# ---------------------------------------------------------------------------


class TestWalkAndVerify:
    def test_clean_chain_yields_no_mismatch(self, tmp_path: Path) -> None:
        agent = tmp_path / "agent-1"
        _populate(agent, 4)
        reader = JournalReader(agent)
        assert list(walk_and_verify(reader)) == []

    def test_is_a_generator_that_streams(self, tmp_path: Path) -> None:
        agent = tmp_path / "agent-1"
        _populate(agent, 4)
        reader = JournalReader(agent)
        # It must be lazy: taking the first result cannot require walking the
        # whole chain into memory first.
        assert inspect.isgenerator(walk_and_verify(reader))

    def test_localizes_tampered_step_hash_to_exact_seq(self, tmp_path: Path) -> None:
        agent = tmp_path / "agent-1"
        _populate(agent, 4)
        _tamper_stored_step_hash(agent, seq=2)

        reader = JournalReader(agent)
        first = next(iter(walk_and_verify(reader)), None)
        assert first is not None
        assert isinstance(first, HashMismatch)
        # First divergence is the tampered digest at seq 2, not the downstream
        # prev_hash break it induces at seq 3.
        assert first.seq == 2
        assert first.first_divergent_field == "step_hash"
        assert first.actual_hash == "f" * 64
        # The expected hash is what the intact fields hash to.
        assert first.expected_hash != first.actual_hash

    def test_localizes_prev_hash_break_and_names_prev_hash(self, tmp_path: Path) -> None:
        agent = tmp_path / "agent-1"
        _populate(agent, 4)
        _tamper_field_keep_prev(agent, seq=1)

        reader = JournalReader(agent)
        first = next(iter(walk_and_verify(reader)), None)
        assert first is not None
        assert first.seq == 1
        assert first.first_divergent_field == "prev_hash"

    def test_first_mismatch_is_the_earliest_step(self, tmp_path: Path) -> None:
        agent = tmp_path / "agent-1"
        _populate(agent, 5)
        # Tamper two steps; the earliest must be the first yielded.
        _tamper_stored_step_hash(agent, seq=3)
        _tamper_stored_step_hash(agent, seq=1)

        reader = JournalReader(agent)
        first = next(iter(walk_and_verify(reader)), None)
        assert first is not None
        assert first.seq == 1


# ---------------------------------------------------------------------------
# two_run_path_diff - divergence localization + determinism
# ---------------------------------------------------------------------------


def _two_runs_diverging_at(tmp_path: Path, seq: int) -> tuple[Path, Path]:
    """Build two 4-step journals that differ only at ``tool_result`` at *seq*."""
    left = tmp_path / "left"
    right = tmp_path / "right"
    jl = Journal.open(left)
    jr = Journal.open(right)
    for i in range(4):
        jl.append(
            input_hash=f"a{i}",
            model="m1",
            prompt=f"step {i}",
            tool_call={"name": "noop"},
            tool_result={"ok": True, "n": i},
        )
        # right diverges only at `seq`: a non-deterministic tool_result.
        jr.append(
            input_hash=f"a{i}",
            model="m1",
            prompt=f"step {i}",
            tool_call={"name": "noop"},
            tool_result={"ok": True, "n": i if i != seq else 999},
        )
    jl.close()
    jr.close()
    return left, right


class TestTwoRunPathDiff:
    def test_localizes_divergence_field_and_seq(self, tmp_path: Path) -> None:
        left, right = _two_runs_diverging_at(tmp_path, seq=2)
        diff = two_run_path_diff(left, right)
        assert isinstance(diff, PathDiff)
        assert diff.diverged is True
        assert diff.divergence is not None
        assert diff.divergence.seq == 2
        assert "tool_result" in diff.divergence.fields_changed
        # The projection runs up to and including the divergence.
        seqs = [s["seq"] for s in diff.steps]
        assert seqs == [0, 1, 2]
        assert diff.steps[-1]["diverged"] is True
        assert "tool_result" in diff.steps[-1]["fields_changed"]

    def test_identical_chains_no_divergence(self, tmp_path: Path) -> None:
        left = tmp_path / "left"
        right = tmp_path / "right"
        _populate(left, 3)
        _populate(right, 3)
        diff = two_run_path_diff(left, right)
        assert diff.diverged is False
        assert diff.divergence is None
        # A stable content address even when nothing diverges.
        assert len(diff.diff_hash) == 64

    def test_content_addressed_diff_is_byte_identical_across_invocations(self, tmp_path: Path) -> None:
        left, right = _two_runs_diverging_at(tmp_path, seq=1)
        a = two_run_path_diff(left, right)
        b = two_run_path_diff(left, right)
        assert a.diff_hash == b.diff_hash
        assert json.dumps(a.to_dict(), sort_keys=True) == json.dumps(b.to_dict(), sort_keys=True)

    def test_diff_hash_is_over_content_not_paths(self, tmp_path: Path) -> None:
        # Two operators with the same journals in different directories must
        # get the byte-identical diff artifact.
        left, right = _two_runs_diverging_at(tmp_path, seq=2)
        diff_a = two_run_path_diff(left, right)

        # Re-create the same chains under different directory names.
        left2, right2 = _two_runs_diverging_at(tmp_path / "op2", seq=2)
        diff_b = two_run_path_diff(left2, right2)
        assert diff_a.diff_hash == diff_b.diff_hash

    def test_diff_hash_matches_recomputed_sha256_of_body(self, tmp_path: Path) -> None:
        import hashlib

        left, right = _two_runs_diverging_at(tmp_path, seq=2)
        diff = two_run_path_diff(left, right)
        body = {"diverged": diff.diverged, "divergence": diff.to_dict()["divergence"], "steps": diff.steps}
        recomputed = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        assert diff.diff_hash == recomputed

    def test_length_mismatch_surfaces_as_divergence(self, tmp_path: Path) -> None:
        left = tmp_path / "left"
        right = tmp_path / "right"
        _populate(left, 3)
        _populate(right, 2)
        diff = two_run_path_diff(left, right)
        assert diff.diverged is True
        assert diff.divergence is not None
        assert diff.divergence.seq == 2
        # The side-by-side marks the shorter side as absent at the missing seq.
        last = diff.steps[-1]
        assert last["seq"] == 2
        assert last["right"] is None
        assert last["left"] is not None


def test_projection_recovers_stored_hashes_from_disk(tmp_path: Path) -> None:
    """The side-by-side projects the exact canonical fields the writer wrote."""
    left, right = _two_runs_diverging_at(tmp_path, seq=1)
    diff = two_run_path_diff(left, right)
    left_reader = JournalReader(left)
    left_entries = {e.seq: e for e in left_reader.entries()}
    for step in diff.steps:
        entry = left_entries[step["seq"]]
        assert step["left"]["prompt"] == entry.prompt
        assert step["left"]["model"] == entry.model
        # prev_hash chains onto the stored value, so a verifier can re-derive
        # the step hash from the projection alone.
        expected = compute_step_hash(
            prev_hash=step["left"]["prev_hash"],
            input_hash=step["left"]["input_hash"],
            model=step["left"]["model"],
            prompt=step["left"]["prompt"],
            tool_call=step["left"]["tool_call"],
            tool_result=step["left"]["tool_result"],
        )
        assert expected == entry.step_hash
