"""A verdict record names the identity it judged (issue #5473, slice 1).

An adjudication record named its judges and never the party whose work they
judged, so "this was reviewed independently" was an assertion nobody could
check from the artefact. `adjudicate()` did not even take a producer argument,
so the information never reached the function: a panel genuinely independent
*of itself*, and identical to the agent that wrote the diff, produced a record
indistinguishable from an independent one.

This slice is deliberately additive and inert. It records the producing
identity inside the signed binding and changes no decision. Deriving the
verdict class from it (slice 2) and refusing a collision (slice 3) are
separate, and nothing here enforces anything.

The load-bearing test is the last one: unset, the new field must leave the
canonical bytes and the spine anchor of every pre-existing record byte-identical.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.quality.adjudication import (
    UNRESOLVED_IDENTITY,
    JudgeConfig,
    JudgeVerdict,
    PanelConfig,
    ProducingIdentity,
    Verdict,
    adjudicate,
    verify_adjudication,
)
from bernstein.core.quality.cross_model_verifier import CrossModelVerdict

_KEY = b"k" * 32


def _judge(model: str, temp: float, prompt: str) -> tuple[JudgeConfig, JudgeVerdict]:
    cfg = JudgeConfig(model=model, temperature=temp, prompt_hash=prompt)
    return cfg, JudgeVerdict(config=cfg, verdict=Verdict.PASS, rationale_hash="r-" + model)


def _panel() -> tuple[PanelConfig, tuple[JudgeVerdict, ...]]:
    cfg_a, verdict_a = _judge("m1", 0.0, "p")
    cfg_b, verdict_b = _judge("m2", 0.2, "q")
    return PanelConfig(judges=(cfg_a, cfg_b)), (verdict_a, verdict_b)


def _verify(root: Path, record):
    return verify_adjudication(
        run_id="run-1",
        lineage_root=root,
        hmac_key=_KEY,
        record=record,
        claimed_inputs={"diff": "one"},
    )


def _adjudicate(root: Path, identity: ProducingIdentity | None = None):
    panel, verdicts = _panel()
    return adjudicate(
        run_id="run-1",
        lineage_root=root,
        hmac_key=_KEY,
        inputs={"diff": "one"},
        rubric={"rule": "r"},
        panel=panel,
        judge_verdicts=verdicts,
        now=1_767_225_600,
        producing_identity=identity,
    )


WRITER = ProducingIdentity(
    adapter="claude",
    model_requested="claude-opus-5",
    model_served="claude-opus-5-20260101",
    family="claude",
)


# ---------------------------------------------------------------------------
# The identity value
# ---------------------------------------------------------------------------


def test_the_identity_carries_the_four_axes_disjointness_is_asked_on() -> None:
    assert WRITER.to_dict() == {
        "adapter": "claude",
        "model_requested": "claude-opus-5",
        "model_served": "claude-opus-5-20260101",
        "family": "claude",
    }


def test_unresolved_served_identity_is_recorded_as_unresolved() -> None:
    """Absence is recorded as absence, never as "distinct".

    #5037 established that the requested and served ids are different facts.
    An unresolvable served id is an unknown, and a later verifier has to be
    able to tell an unknown from a mismatch -- so the field is filled with a
    sentinel rather than left empty or dropped.
    """
    identity = ProducingIdentity(adapter="claude", model_requested="claude-opus-5")

    assert identity.model_served == UNRESOLVED_IDENTITY
    assert identity.family == UNRESOLVED_IDENTITY
    assert identity.to_dict()["model_served"] == UNRESOLVED_IDENTITY


@pytest.mark.parametrize(
    "kwargs",
    [
        {"adapter": "", "model_requested": "m"},
        {"adapter": "a", "model_requested": ""},
        {"adapter": "a", "model_requested": "m", "model_served": ""},
        {"adapter": "a", "model_requested": "m", "family": ""},
    ],
)
def test_an_empty_axis_is_refused(kwargs: dict[str, str]) -> None:
    """Empty would be a third state between "known" and "unresolved"."""
    with pytest.raises(ValueError):
        ProducingIdentity(**kwargs)


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------


def test_verdict_record_names_the_producing_identity(tmp_path: Path) -> None:
    """In the signed binding, not beside it.

    Beside it would leave the identity unanchored: anyone could edit it after
    the fact without the spine noticing, which is the opposite of the property
    this issue asks for.
    """
    record = _adjudicate(tmp_path, WRITER)

    assert record.producing_identity == WRITER
    assert record.to_canonical_bytes().count(b"producing_identity") == 1
    assert b"claude-opus-5-20260101" in record.to_canonical_bytes()
    assert record.to_dict()["producing_identity"] == WRITER.to_dict()
    assert _verify(tmp_path, record).ok is True


def test_a_record_without_the_identity_reads_as_unattributed(tmp_path: Path) -> None:
    """Not as independent.

    The key is absent rather than null, so a reader distinguishes "written
    before this field existed" from "declared to have no producer".
    """
    record = _adjudicate(tmp_path)

    assert record.producing_identity is None
    assert "producing_identity" not in record.to_dict()
    assert b"producing_identity" not in record.to_canonical_bytes()


def test_the_anchored_record_keeps_the_identity(tmp_path: Path) -> None:
    """`adjudicate` rebuilds the record to attach the anchor.

    A field dropped in that rebuild would be recorded in the spine bytes and
    then absent from the object the caller holds, which is the worst of both.
    """
    record = _adjudicate(tmp_path, WRITER)

    assert record.journal_entry_hash != ""
    assert record.producing_identity == WRITER


def test_the_identity_is_inside_what_the_anchor_commits_to(tmp_path: Path) -> None:
    """Changing the producer must change the anchor."""
    with_writer = _adjudicate(tmp_path, WRITER)
    other = ProducingIdentity(
        adapter="codex",
        model_requested="gpt-5.3-codex",
        model_served="gpt-5.3-codex-20260101",
        family="gpt",
    )
    with_other = _adjudicate(tmp_path, other)

    assert with_writer.to_canonical_bytes() != with_other.to_canonical_bytes()
    assert with_writer.journal_entry_hash != with_other.journal_entry_hash


def test_pre_change_record_bytes_and_anchor_are_unchanged(tmp_path: Path) -> None:
    """The load-bearing one: unset, the field costs nothing.

    Every adjudication record written before this change must keep its exact
    canonical bytes and its exact spine anchor, or the field would invalidate
    the history it was added to make checkable.

    The expected bytes are written out literally rather than recomputed from
    the record, because recomputing would move both sides of the comparison
    together and prove nothing.
    """
    record = _adjudicate(tmp_path)

    expected = (
        b'{"final_verdict":"pass",'
        b'"inputs_hash":"' + record.inputs_hash.encode() + b'",'
        b'"panel_config":' + _canonical(record.panel_config) + b","
        b'"per_judge_verdict":' + _canonical(list(record.per_judge_verdict)) + b","
        b'"rubric_hash":"' + record.rubric_hash.encode() + b'",'
        b'"run_id":"run-1",'
        b'"timestamp":1767225600}'
    )
    assert record.to_canonical_bytes() == expected
    # And the untouched bytes still verify against the spine they were
    # anchored in, which is the half that would break if the field leaked in.
    assert _verify(tmp_path, record).ok is True


def _canonical(value: object) -> bytes:
    import json

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def test_two_identical_runs_still_produce_identical_records(tmp_path: Path) -> None:
    """Determinism is unaffected, with the field set and unset."""
    a = _adjudicate(tmp_path / "a")
    b = _adjudicate(tmp_path / "b")
    assert a.to_canonical_bytes() == b.to_canonical_bytes()

    c = _adjudicate(tmp_path / "c", WRITER)
    d = _adjudicate(tmp_path / "d", WRITER)
    assert c.to_canonical_bytes() == d.to_canonical_bytes()


# ---------------------------------------------------------------------------
# The cross-model verdict
# ---------------------------------------------------------------------------


def test_cross_model_verdict_records_both_sides() -> None:
    verdict = CrossModelVerdict(
        verdict="approve",
        feedback="ok",
        reviewer_model="gpt-5.3",
        writer_model="claude-opus-5",
    )

    assert verdict.reviewer_model == "gpt-5.3"
    assert verdict.writer_model == "claude-opus-5"


def test_cross_model_verdict_writer_defaults_to_unrecorded() -> None:
    """Empty means unknown, and a reader must not read it as "differs"."""
    verdict = CrossModelVerdict(verdict="approve", feedback="ok", reviewer_model="gpt-5.3")

    assert verdict.writer_model == ""
