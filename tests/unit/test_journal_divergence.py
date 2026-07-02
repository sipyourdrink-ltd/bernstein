"""Tests for ``bernstein.core.persistence.journal_diff`` (#1799).

When two replays diverge the orchestrator must surface *which field
flipped* - prompt, model, tool_call, tool_result - rather than a flaky
test signature. These tests pin that contract.
"""

from __future__ import annotations

from pathlib import Path

from bernstein.core.persistence.journal import Journal
from bernstein.core.persistence.journal_diff import (
    StepDivergence,
    diff_journals,
    diff_steps,
)


class TestStepDiff:
    def test_identical_steps_no_divergence(self) -> None:
        entry_a = {
            "prev_hash": "0" * 64,
            "input_hash": "aa",
            "model": "m1",
            "prompt": "hi",
            "tool_call": {"name": "echo"},
            "tool_result": {"ok": True},
        }
        result = diff_steps(entry_a, dict(entry_a))
        assert result is None

    def test_single_field_change_named_in_diff(self) -> None:
        entry_a = {
            "prev_hash": "0" * 64,
            "input_hash": "aa",
            "model": "m1",
            "prompt": "hi",
            "tool_call": {"name": "echo"},
            "tool_result": {"ok": True},
        }
        entry_b = dict(entry_a, model="m2")
        result = diff_steps(entry_a, entry_b)
        assert result is not None
        assert "model" in result.fields_changed
        assert result.left_values["model"] == "m1"
        assert result.right_values["model"] == "m2"

    def test_multiple_field_changes_all_named(self) -> None:
        entry_a = {
            "prev_hash": "0" * 64,
            "input_hash": "aa",
            "model": "m1",
            "prompt": "hi",
            "tool_call": {"name": "echo"},
            "tool_result": {"ok": True},
        }
        entry_b = dict(entry_a, model="m2", tool_result={"ok": False})
        result = diff_steps(entry_a, entry_b)
        assert result is not None
        assert set(result.fields_changed) == {"model", "tool_result"}


class TestJournalDiff:
    def test_two_identical_chains_no_divergence(self, tmp_path: Path) -> None:
        for name in ("a", "b"):
            journal = Journal.open(tmp_path / name)
            for i in range(3):
                journal.append(input_hash=f"a{i}", model="m1", prompt=f"p{i}")
            journal.close()
        result = diff_journals(tmp_path / "a", tmp_path / "b")
        assert result is None

    def test_divergence_at_step_reports_exact_field(self, tmp_path: Path) -> None:
        journal_a = Journal.open(tmp_path / "a")
        for i in range(3):
            journal_a.append(input_hash=f"a{i}", model="m1", prompt=f"p{i}")
        journal_a.close()

        journal_b = Journal.open(tmp_path / "b")
        journal_b.append(input_hash="a0", model="m1", prompt="p0")
        # Step 1 diverges: model flipped.
        journal_b.append(input_hash="a1", model="m2", prompt="p1")
        journal_b.append(input_hash="a2", model="m1", prompt="p2")
        journal_b.close()

        result = diff_journals(tmp_path / "a", tmp_path / "b")
        assert result is not None
        assert result.seq == 1
        assert "model" in result.fields_changed
        assert result.left_values["model"] == "m1"
        assert result.right_values["model"] == "m2"

    def test_chain_length_mismatch_reported(self, tmp_path: Path) -> None:
        journal_a = Journal.open(tmp_path / "a")
        journal_a.append(input_hash="a0", model="m1", prompt="p0")
        journal_a.append(input_hash="a1", model="m1", prompt="p1")
        journal_a.close()

        journal_b = Journal.open(tmp_path / "b")
        journal_b.append(input_hash="a0", model="m1", prompt="p0")
        journal_b.close()

        result = diff_journals(tmp_path / "a", tmp_path / "b")
        assert result is not None
        # The first missing step is at seq=1 on b.
        assert result.seq == 1
        assert "length" in result.reason.lower() or "missing" in result.reason.lower()

    def test_divergence_dataclass_is_hashable(self) -> None:
        """The orchestrator stuffs divergences into sets when surfacing
        flaky-test root causes, so the dataclass must be hashable."""
        d = StepDivergence(
            seq=0,
            fields_changed=("model",),
            left_values={"model": "m1"},
            right_values={"model": "m2"},
            reason="model differs",
        )
        assert hash(d) == hash(d)


class TestEffortDivergence:
    """Two chains differing only in effort must surface a named ``effort`` diff.

    Regression: ``_HASHED_FIELDS`` omitted ``effort`` while ``effort`` folds
    into the step hash, so two runs identical except for reasoning effort had
    divergent step hashes yet ``diff_journals`` reported no divergence - the
    exact hash-soup blindness the module exists to prevent.
    """

    def test_effort_only_change_named_in_step_diff(self) -> None:
        entry_a = {
            "prev_hash": "0" * 64,
            "input_hash": "aa",
            "model": "m1",
            "prompt": "hi",
            "tool_call": None,
            "tool_result": None,
            "effort": "low",
        }
        entry_b = dict(entry_a, effort="high")
        result = diff_steps(entry_a, entry_b)
        assert result is not None
        assert "effort" in result.fields_changed
        assert result.left_values["effort"] == "low"
        assert result.right_values["effort"] == "high"

    def test_effort_only_change_surfaces_in_diff_journals(self, tmp_path: Path) -> None:
        journal_a = Journal.open(tmp_path / "a")
        journal_a.append(input_hash="a0", model="m1", prompt="p0", effort="low")
        journal_a.close()

        journal_b = Journal.open(tmp_path / "b")
        journal_b.append(input_hash="a0", model="m1", prompt="p0", effort="high")
        journal_b.close()

        result = diff_journals(tmp_path / "a", tmp_path / "b")
        assert result is not None, "an effort-only divergence must be reported"
        assert result.seq == 0
        assert "effort" in result.fields_changed
        assert result.left_values["effort"] == "low"
        assert result.right_values["effort"] == "high"
