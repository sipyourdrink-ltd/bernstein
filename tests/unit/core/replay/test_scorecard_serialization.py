"""Tests for ``bernstein.core.replay.scorecard`` canonical serialization.

Scorecards must serialise deterministically: two independently built
Scorecards whose ``to_dict()`` outputs compare equal must produce
byte-identical :meth:`Scorecard.canonical_bytes` output, regardless of
section field ordering, construction order, or whether the source was
materialised from the dataclass directly or via the ``from_dict``
round-trip.

The encoding convention (canonical JSON, sorted keys, ``(",", ":")``
separators, UTF-8, wall-clock stripped) is shared with
:mod:`bernstein.core.replay.run_receipt` and the audit-receipt family
- these tests pin that contract down for the scorecard specifically.
"""

from __future__ import annotations

import json

import pytest

from bernstein.core.replay.scorecard import (
    SCORECARD_SCHEMA_VERSION,
    SCORECARD_TYPE,
    SCORECARD_TYPE_VERSION,
    Citation,
    RecoverySection,
    ReplayabilitySection,
    SafetySection,
    Scorecard,
    StateConsistencySection,
    TrajectorySection,
    VerificationSection,
)

# ---------------------------------------------------------------------------
# Fixtures - valid scorecard dicts (one "full" form, one "minimal" form)
# ---------------------------------------------------------------------------


def _valid_trajectory_dict() -> dict:
    return {
        "step_count": 4,
        "first_step_index": 0,
        "last_step_index": 3,
        "first_step_hash": "h0" * 32,
        "last_step_hash": "h3" * 32,
        "schema_version": 1,
    }


def _valid_verification_dict() -> dict:
    return {
        "journal_ok": True,
        "journal_head": "j" * 64,
        "journal_steps": 4,
        "spine_ok": True,
        "spine_head": "s" * 64,
        "spine_entries": 2,
    }


def _valid_recovery_dict() -> dict:
    return {
        "repaired": False,
        "dropped_rows": 0,
    }


def _valid_state_consistency_dict() -> dict:
    return {
        "mutation_count": 1,
        "disagreement_count": 0,
        "last_mutation_event_index": 3,
    }


def _valid_safety_dict() -> dict:
    return {
        "capability_declared": True,
        "refusal_count": 0,
        "run_receipt_signed": True,
    }


def _valid_replayability_dict() -> dict:
    return {
        "recorded": True,
        "key_scheme": "scheme/v1",
        "gateway_mode": "record",
        "fixture_present": True,
    }


def full_scorecard_dict() -> dict:
    """A fully populated scorecard dict, every field present."""
    return {
        "run_id": "run-full",
        "schema_version": SCORECARD_SCHEMA_VERSION,
        "type_version": SCORECARD_TYPE_VERSION,
        "scorecard_type": SCORECARD_TYPE,
        "trajectory": _valid_trajectory_dict(),
        "verification": _valid_verification_dict(),
        "recovery": _valid_recovery_dict(),
        "state_consistency": _valid_state_consistency_dict(),
        "safety": _valid_safety_dict(),
        "replayability": _valid_replayability_dict(),
    }


def minimal_scorecard_dict() -> dict:
    """A minimal scorecard dict - empty journal, no provider state."""
    return {
        "run_id": "run-empty",
        "trajectory": {"step_count": 0},
        "verification": {
            "journal_ok": True,
            "journal_head": "",
            "journal_steps": 0,
            "spine_ok": True,
            "spine_head": "",
            "spine_entries": 0,
        },
        "recovery": {"repaired": False, "dropped_rows": 0},
        "state_consistency": {"mutation_count": 0, "disagreement_count": 0},
        "safety": {
            "capability_declared": False,
            "refusal_count": 0,
            "run_receipt_signed": False,
        },
        "replayability": {
            "recorded": False,
            "key_scheme": "",
            "gateway_mode": "",
            "fixture_present": False,
        },
    }


def build_full_scorecard() -> Scorecard:
    return Scorecard(
        run_id="run-full",
        trajectory=TrajectorySection(
            step_count=4,
            first_step_index=0,
            last_step_index=3,
            first_step_hash="h0" * 32,
            last_step_hash="h3" * 32,
            citations=(),
        ),
        verification=VerificationSection(
            journal_ok=True,
            journal_head="j" * 64,
            journal_steps=4,
            divergent_step=None,
            spine_ok=True,
            spine_head="s" * 64,
            spine_entries=2,
            citations=(),
        ),
        recovery=RecoverySection(
            repaired=False,
            dropped_rows=0,
            first_recoverable_seq=None,
            recovery_event_index=None,
            citations=(),
        ),
        state_consistency=StateConsistencySection(
            mutation_count=1,
            disagreement_count=0,
            last_mutation_event_index=3,
            citations=(),
        ),
        safety=SafetySection(
            capability_declared=True,
            refusal_count=0,
            run_receipt_signed=True,
            citations=(),
        ),
        replayability=ReplayabilitySection(
            recorded=True,
            key_scheme="scheme/v1",
            gateway_mode="record",
            fixture_present=True,
            citations=(),
        ),
        wall_clock_start=1234567890.0,
        wall_clock_end=1234567990.0,
    )


# ---------------------------------------------------------------------------
# Valid canonical-bytes tests
# ---------------------------------------------------------------------------


class TestCanonicalBytes:
    """Pin the canonical-bytes contract for Scorecard serialization."""

    def test_canonical_bytes_are_utf8(self) -> None:
        scorecard = Scorecard.from_dict(full_scorecard_dict())
        assert isinstance(scorecard.canonical_bytes(), bytes)

    def test_canonical_bytes_omit_wall_clock(self) -> None:
        scorecard = build_full_scorecard()
        blob = scorecard.canonical_bytes()
        text = blob.decode("utf-8")
        # Wall-clock fields must never enter the canonical bytes.
        assert "wall_clock_start" not in text
        assert "wall_clock_end" not in text
        assert "1234567890" not in text
        assert "1234567990" not in text

    def test_canonical_bytes_match_sorted_compact_json(self) -> None:
        scorecard = Scorecard.from_dict(full_scorecard_dict())
        expected = json.dumps(
            scorecard.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        assert scorecard.canonical_bytes() == expected

    def test_canonical_bytes_use_compact_separators(self) -> None:
        scorecard = Scorecard.from_dict(full_scorecard_dict())
        text = scorecard.canonical_bytes().decode("utf-8")
        # No spaces between JSON tokens; no spaces after ``:`` or ``,``.
        assert ": " not in text
        assert ", " not in text

    def test_keys_are_sorted_in_canonical_bytes(self) -> None:
        scorecard = Scorecard.from_dict(full_scorecard_dict())
        text = scorecard.canonical_bytes().decode("utf-8")
        # Sorted top-level keys: recovery, replayability, run_id, safety,
        # schema_version, scorecard_type, state_consistency, trajectory,
        # type_version, verification. Verify the entire sequence is in
        # ascending lexicographic order.
        assert (
            text.index('"recovery"')
            < text.index('"replayability"')
            < text.index('"run_id"')
            < text.index('"safety"')
            < text.index('"schema_version"')
            < text.index('"scorecard_type"')
            < text.index('"state_consistency"')
            < text.index('"trajectory"')
            < text.index('"type_version"')
            < text.index('"verification"')
        )

    def test_independent_constructions_produce_byte_identical_bytes(self) -> None:
        """Two Scorecards built from the same dict must be byte-identical."""
        scorecard_a = Scorecard.from_dict(full_scorecard_dict())
        scorecard_b = Scorecard.from_dict(full_scorecard_dict())
        assert scorecard_a.canonical_bytes() == scorecard_b.canonical_bytes()

    def test_construction_order_independence(self) -> None:
        """Build the same scorecard two different ways; bytes must match.

        One path goes through the dataclass constructor with positional
        sections in canonical order; the other goes through ``from_dict``
        with the dict's section-order-independent content. The canonical
        bytes must still match because the encoding is sorted by key.
        """
        canonical = build_full_scorecard().canonical_bytes()
        via_dict = Scorecard.from_dict(full_scorecard_dict()).canonical_bytes()
        assert canonical == via_dict

    def test_section_field_insertion_order_does_not_matter(self) -> None:
        """Reorder the section dict; canonical bytes stay identical.

        The ``to_dict`` keys are written by hand in a fixed order, but
        the ``canonical_bytes`` encoding is sorted by key, so any input
        ordering to ``from_dict`` must produce the same wire shape.
        """
        reordered: dict = {
            "scorecard_type": SCORECARD_TYPE,
            "type_version": SCORECARD_TYPE_VERSION,
            "schema_version": SCORECARD_SCHEMA_VERSION,
            "trajectory": _valid_trajectory_dict(),
            "replayability": _valid_replayability_dict(),
            "recovery": _valid_recovery_dict(),
            "safety": _valid_safety_dict(),
            "verification": _valid_verification_dict(),
            "state_consistency": _valid_state_consistency_dict(),
            "run_id": "run-full",
        }
        reference = Scorecard.from_dict(full_scorecard_dict()).canonical_bytes()
        assert Scorecard.from_dict(reordered).canonical_bytes() == reference

    def test_inner_section_field_insertion_order_does_not_matter(self) -> None:
        """Reorder inner section dict fields; bytes stay identical."""
        d = full_scorecard_dict()
        traj = d["trajectory"]
        d["trajectory"] = {
            "schema_version": traj["schema_version"],
            "last_step_hash": traj["last_step_hash"],
            "first_step_hash": traj["first_step_hash"],
            "step_count": traj["step_count"],
            "last_step_index": traj["last_step_index"],
            "first_step_index": traj["first_step_index"],
        }
        reference = Scorecard.from_dict(full_scorecard_dict()).canonical_bytes()
        assert Scorecard.from_dict(d).canonical_bytes() == reference

    def test_round_trip_through_from_dict_preserves_canonical_bytes(self) -> None:
        """``to_dict`` -> ``from_dict`` -> ``canonical_bytes`` is fixed point."""
        original = build_full_scorecard()
        rebuilt = Scorecard.from_dict(original.to_dict())
        assert original.canonical_bytes() == rebuilt.canonical_bytes()

    def test_minimal_scorecard_round_trip(self) -> None:
        """Empty sections still round-trip without changing bytes."""
        original = Scorecard.from_dict(minimal_scorecard_dict())
        rebuilt = Scorecard.from_dict(original.to_dict())
        assert original.canonical_bytes() == rebuilt.canonical_bytes()


# ---------------------------------------------------------------------------
# Section-level canonical-bytes tests
# ---------------------------------------------------------------------------


class TestSectionCanonicalBytes:
    """Each section's to_dict must round-trip through from_dict."""

    def test_trajectory_section_round_trip(self) -> None:
        raw = _valid_trajectory_dict()
        section = TrajectorySection.from_dict(raw)
        assert section.to_dict() == raw

    def test_verification_section_round_trip(self) -> None:
        raw = _valid_verification_dict()
        section = VerificationSection.from_dict(raw)
        assert section.to_dict() == raw

    def test_recovery_section_round_trip(self) -> None:
        raw = _valid_recovery_dict()
        section = RecoverySection.from_dict(raw)
        assert section.to_dict() == raw

    def test_state_consistency_section_round_trip(self) -> None:
        raw = _valid_state_consistency_dict()
        section = StateConsistencySection.from_dict(raw)
        assert section.to_dict() == raw

    def test_safety_section_round_trip(self) -> None:
        raw = _valid_safety_dict()
        section = SafetySection.from_dict(raw)
        assert section.to_dict() == raw

    def test_replayability_section_round_trip(self) -> None:
        raw = _valid_replayability_dict()
        section = ReplayabilitySection.from_dict(raw)
        assert section.to_dict() == raw

    def test_trajectory_omits_none_fields(self) -> None:
        section = TrajectorySection(
            step_count=0,
            first_step_index=None,
            last_step_index=None,
            first_step_hash=None,
            last_step_hash=None,
        )
        wire = section.to_dict()
        assert "first_step_index" not in wire
        assert "last_step_index" not in wire
        assert "first_step_hash" not in wire
        assert "last_step_hash" not in wire

    def test_verification_omits_none_divergent_step(self) -> None:
        section = VerificationSection(
            journal_ok=True,
            journal_head="",
            journal_steps=0,
            divergent_step=None,
            spine_ok=True,
            spine_head="",
            spine_entries=0,
        )
        wire = section.to_dict()
        assert "divergent_step" not in wire

    def test_recovery_omits_none_optional_fields(self) -> None:
        section = RecoverySection(
            repaired=False,
            dropped_rows=0,
            first_recoverable_seq=None,
            recovery_event_index=None,
        )
        wire = section.to_dict()
        assert "first_recoverable_seq" not in wire
        assert "recovery_event_index" not in wire

    def test_state_consistency_omits_none_mutation_event_index(self) -> None:
        section = StateConsistencySection(
            mutation_count=0,
            disagreement_count=0,
            last_mutation_event_index=None,
        )
        wire = section.to_dict()
        assert "last_mutation_event_index" not in wire


# ---------------------------------------------------------------------------
# Citation round-trip
# ---------------------------------------------------------------------------


class TestCitationRoundTrip:
    def test_citation_with_indices_round_trips(self) -> None:
        c = Citation(
            journal_event_index=7,
            step_hash="abc",
            section="trajectory",
            field="first_step_hash",
        )
        raw = c.to_dict()
        assert Citation.from_dict(raw) == c

    def test_citation_omits_none_optional_fields(self) -> None:
        c = Citation(
            journal_event_index=None,
            step_hash=None,
            section="safety",
            field="refusal_count",
        )
        raw = c.to_dict()
        assert "journal_event_index" not in raw
        assert "step_hash" not in raw
        assert Citation.from_dict(raw) == c


# ---------------------------------------------------------------------------
# Invalid serialization fixtures - from_dict must reject, not silently coerce
# ---------------------------------------------------------------------------


class TestFromDictRejectsMalformed:
    """from_dict must reject malformed inputs rather than silently coerce.

    A verifier that round-trips a scorecard through ``from_dict`` must
    never end up with a structurally different scorecard than the one
    ``to_dict`` produced.
    """

    def test_missing_run_id_raises(self) -> None:
        d = full_scorecard_dict()
        d.pop("run_id")
        with pytest.raises(KeyError):
            Scorecard.from_dict(d)

    def test_missing_trajectory_raises(self) -> None:
        d = full_scorecard_dict()
        d.pop("trajectory")
        with pytest.raises(KeyError):
            Scorecard.from_dict(d)

    def test_missing_verification_raises(self) -> None:
        d = full_scorecard_dict()
        d.pop("verification")
        with pytest.raises(KeyError):
            Scorecard.from_dict(d)

    def test_missing_recovery_raises(self) -> None:
        d = full_scorecard_dict()
        d.pop("recovery")
        with pytest.raises(KeyError):
            Scorecard.from_dict(d)

    def test_missing_state_consistency_raises(self) -> None:
        d = full_scorecard_dict()
        d.pop("state_consistency")
        with pytest.raises(KeyError):
            Scorecard.from_dict(d)

    def test_missing_safety_raises(self) -> None:
        d = full_scorecard_dict()
        d.pop("safety")
        with pytest.raises(KeyError):
            Scorecard.from_dict(d)

    def test_missing_replayability_raises(self) -> None:
        d = full_scorecard_dict()
        d.pop("replayability")
        with pytest.raises(KeyError):
            Scorecard.from_dict(d)

    def test_non_dict_input_raises(self) -> None:
        with pytest.raises((TypeError, AttributeError)):
            Scorecard.from_dict([])  # type: ignore[arg-type]

    def test_citation_from_non_dict_raises(self) -> None:
        with pytest.raises((TypeError, AttributeError, KeyError)):
            Citation.from_dict(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Convention tie-in with run_receipt
# ---------------------------------------------------------------------------


class TestRunReceiptConventionTie:
    """Pin the rule that scorecard canonicalisation matches run_receipt.

    The ``canonical_bytes`` encoding used by :class:`Scorecard` is the
    same convention :func:`bernstein.core.replay.run_receipt._canonical_json_bytes`
    uses for the signed run-receipt payload: canonical JSON, sorted
    keys, ``(",", ":")`` separators, UTF-8. A divergence between the
    two is a wire-format break.
    """

    def test_scorecard_canonical_matches_run_receipt_helper(self) -> None:
        from bernstein.core.replay.run_receipt import _canonical_json_bytes

        scorecard = Scorecard.from_dict(full_scorecard_dict())
        expected = _canonical_json_bytes(scorecard.to_dict())
        assert scorecard.canonical_bytes() == expected
