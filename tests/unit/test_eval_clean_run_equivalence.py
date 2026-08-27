"""Tests for verify_equivalence_attestation and _load_equivalence_journal in eval/clean_run."""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.core.lineage.spine import LineageSpine
from bernstein.core.replay.journal import EventJournal, load_events
from bernstein.eval.clean_run import (
    EVAL_CLEAN_RUN_RUN_ID,
    EquivalenceAttestation,
    EquivalenceVerdict,
    _hash_obj,
    _load_equivalence_journal,
    clean_run_attestation_path,
    verify_equivalence_attestation,
)
from tests.unit.test_eval_clean_run import _KEY

_TS = 1_700_000_000
_RUN_ID = "golden-fib-001-equivalence"
_ORIGINAL_RUN_ID = "golden-fib-001"


def _seed_journal(run_id: str, tmp_path: Path, rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """Record *rows* into a real Merkle-chained journal and load them back."""
    journal = EventJournal(run_id, tmp_path / ".sdd")
    for row in rows:
        event = str(row.pop("event"))
        journal.record(event, **row)
    return load_events(journal.path).events


def _clean_rows() -> list[dict[str, object]]:
    return [
        {"event": "file_read", "path": "src/mathlib.py", "content_window": "def add(a, b): return a + b"},
        {"event": "tool_call", "arguments": {"command": "pytest -q"}},
        {"event": "network_egress", "endpoint": "127.0.0.1:8052"},
    ]


def _write_attestation(
    tmp_path: Path,
    attestation: EquivalenceAttestation,
) -> None:
    """Write an attestation to disk in the expected location."""
    path = clean_run_attestation_path(tmp_path, attestation.attestation_hash)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(attestation.to_dict(), ensure_ascii=False), encoding="utf-8")


def _anchor_in_spine(
    tmp_path: Path,
    attestation: EquivalenceAttestation,
) -> tuple[EquivalenceAttestation, str]:
    """Anchor the attestation in the lineage spine and return the attestation with correct journal_entry_hash."""
    lineage_root = tmp_path / ".sdd" / "lineage"
    lineage_root.mkdir(parents=True, exist_ok=True)
    spine = LineageSpine(lineage_root, run_id=EVAL_CLEAN_RUN_RUN_ID, hmac_key=_KEY)
    content = attestation.canonical_bytes()
    entry_hash = spine.record(
        artifact_path=".sdd/eval/clean_run",
        content=content,
        actor="test-actor",
        step_id="step-1",
        model="test-model",
        timestamp=_TS,
    )
    attestation = EquivalenceAttestation(
        **{**attestation.to_dict(), "journal_entry_hash": entry_hash},
    )
    return attestation, entry_hash


# ---------------------------------------------------------------------------
# Tests for _load_equivalence_journal
# ---------------------------------------------------------------------------


def test_load_equivalence_journal_returns_empty_for_unknown_run_id(tmp_path: Path) -> None:
    """_load_equivalence_journal returns [] for an unknown run_id (JournalPathError path)."""
    result = _load_equivalence_journal(tmp_path, "nonexistent-run-id", "original")
    assert result == []

    result = _load_equivalence_journal(tmp_path, "nonexistent-run-id", "substituted")
    assert result == []


def test_load_equivalence_journal_loads_real_events_for_original(tmp_path: Path) -> None:
    """_load_equivalence_journal loads real events for original journal type."""
    rows = _clean_rows()
    events = _seed_journal(_ORIGINAL_RUN_ID, tmp_path, [dict(r) for r in rows])

    result = _load_equivalence_journal(tmp_path, f"{_ORIGINAL_RUN_ID}-equivalence", "original")
    assert len(result) == len(events)
    assert result[0]["event"] == "file_read"


def test_load_equivalence_journal_loads_real_events_for_substituted(tmp_path: Path) -> None:
    """_load_equivalence_journal loads real events for substituted journal type."""
    rows = _clean_rows()
    events = _seed_journal(_RUN_ID, tmp_path, [dict(r) for r in rows])

    result = _load_equivalence_journal(tmp_path, _RUN_ID, "substituted")
    assert len(result) == len(events)
    assert result[0]["event"] == "file_read"


# ---------------------------------------------------------------------------
# Tests for verify_equivalence_attestation
# ---------------------------------------------------------------------------


def test_verify_equivalence_attestation_intact_equivalent_verifies_ok(tmp_path: Path) -> None:
    """Intact attestation with matching original+substituted heads and EQUIVALENT verdict verifies ok=True."""
    rows = _clean_rows()
    original_events = _seed_journal(_ORIGINAL_RUN_ID, tmp_path, [dict(r) for r in rows])
    substituted_events = _seed_journal(_RUN_ID, tmp_path, [dict(r) for r in rows])

    original_head = original_events[-1]["event_hash"]
    substituted_head = substituted_events[-1]["event_hash"]

    assert original_head == substituted_head

    body = {
        "schema_version": 1,
        "run_id": _RUN_ID,
        "original_journal_head": original_head,
        "substituted_journal_head": substituted_head,
        "first_divergent_step": None,
        "substitution_label": "test-substitution",
        "verdict": EquivalenceVerdict.EQUIVALENT.value,
        "timestamp": _TS,
    }
    attestation_hash = _hash_obj(body)

    attestation = EquivalenceAttestation(
        schema_version=1,
        run_id=_RUN_ID,
        original_journal_head=original_head,
        substituted_journal_head=substituted_head,
        first_divergent_step=None,
        substitution_label="test-substitution",
        verdict=EquivalenceVerdict.EQUIVALENT.value,
        timestamp=_TS,
        attestation_hash=attestation_hash,
    )

    attestation, _ = _anchor_in_spine(tmp_path, attestation)
    _write_attestation(tmp_path, attestation)

    result = verify_equivalence_attestation(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        attestation_hash=attestation_hash,
        original_journal_events=original_events,
        substituted_journal_events=substituted_events,
    )

    assert result.ok is True
    assert result.reason == ""
    assert result.attestation is not None
    assert result.attestation.attestation_hash == attestation_hash


def test_verify_equivalence_attestation_original_journal_unavailable(tmp_path: Path) -> None:
    """original_journal_events empty/None with no journal on disk -> ok=False."""
    rows = _clean_rows()
    substituted_events = _seed_journal(_RUN_ID, tmp_path, [dict(r) for r in rows])
    substituted_head = substituted_events[-1]["event_hash"]

    body = {
        "schema_version": 1,
        "run_id": _RUN_ID,
        "original_journal_head": "0" * 64,
        "substituted_journal_head": substituted_head,
        "first_divergent_step": None,
        "substitution_label": "test-substitution",
        "verdict": EquivalenceVerdict.DIVERGED.value,
        "timestamp": _TS,
    }
    attestation_hash = _hash_obj(body)

    attestation = EquivalenceAttestation(
        schema_version=1,
        run_id=_RUN_ID,
        original_journal_head="0" * 64,
        substituted_journal_head=substituted_head,
        first_divergent_step=None,
        substitution_label="test-substitution",
        verdict=EquivalenceVerdict.DIVERGED.value,
        timestamp=_TS,
        attestation_hash=attestation_hash,
    )

    _write_attestation(tmp_path, attestation)

    result = verify_equivalence_attestation(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        attestation_hash=attestation_hash,
    )

    assert result.ok is False
    assert "original run journal unavailable" in result.reason


def test_verify_equivalence_attestation_original_journal_chain_broken(tmp_path: Path) -> None:
    """Original journal chain broken (tamper an event_hash) -> ok=False."""
    rows = _clean_rows()
    original_events = _seed_journal(_ORIGINAL_RUN_ID, tmp_path, [dict(r) for r in rows])
    original_events[1]["event_hash"] = "a" * 64
    substituted_events = _seed_journal(_RUN_ID, tmp_path, [dict(r) for r in rows])

    original_head = original_events[-1]["event_hash"]
    substituted_head = substituted_events[-1]["event_hash"]

    body = {
        "schema_version": 1,
        "run_id": _RUN_ID,
        "original_journal_head": original_head,
        "substituted_journal_head": substituted_head,
        "first_divergent_step": 1,
        "substitution_label": "test-substitution",
        "verdict": EquivalenceVerdict.DIVERGED.value,
        "timestamp": _TS,
    }
    attestation_hash = _hash_obj(body)

    attestation = EquivalenceAttestation(
        schema_version=1,
        run_id=_RUN_ID,
        original_journal_head=original_head,
        substituted_journal_head=substituted_head,
        first_divergent_step=1,
        substitution_label="test-substitution",
        verdict=EquivalenceVerdict.DIVERGED.value,
        timestamp=_TS,
        attestation_hash=attestation_hash,
    )

    _write_attestation(tmp_path, attestation)

    result = verify_equivalence_attestation(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        attestation_hash=attestation_hash,
        original_journal_events=original_events,
        substituted_journal_events=substituted_events,
    )

    assert result.ok is False
    assert "original run journal chain broken" in result.reason


def test_verify_equivalence_attestation_original_head_mismatch(tmp_path: Path) -> None:
    """Original journal last event_hash != recorded original_journal_head -> ok=False."""
    rows = _clean_rows()
    original_events = _seed_journal(_ORIGINAL_RUN_ID, tmp_path, [dict(r) for r in rows])
    substituted_events = _seed_journal(_RUN_ID, tmp_path, [dict(r) for r in rows])

    substituted_head = substituted_events[-1]["event_hash"]

    body = {
        "schema_version": 1,
        "run_id": _RUN_ID,
        "original_journal_head": "b" * 64,
        "substituted_journal_head": substituted_head,
        "first_divergent_step": 1,
        "substitution_label": "test-substitution",
        "verdict": EquivalenceVerdict.DIVERGED.value,
        "timestamp": _TS,
    }
    attestation_hash = _hash_obj(body)

    attestation = EquivalenceAttestation(
        schema_version=1,
        run_id=_RUN_ID,
        original_journal_head="b" * 64,
        substituted_journal_head=substituted_head,
        first_divergent_step=1,
        substitution_label="test-substitution",
        verdict=EquivalenceVerdict.DIVERGED.value,
        timestamp=_TS,
        attestation_hash=attestation_hash,
    )

    _write_attestation(tmp_path, attestation)

    result = verify_equivalence_attestation(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        attestation_hash=attestation_hash,
        original_journal_events=original_events,
        substituted_journal_events=substituted_events,
    )

    assert result.ok is False
    assert "original_journal_head" in result.reason


def test_verify_equivalence_attestation_substituted_journal_unavailable(tmp_path: Path) -> None:
    """Substituted journal unavailable -> ok=False reason mentions substituted run journal unavailable."""
    rows = _clean_rows()
    original_events = _seed_journal(_ORIGINAL_RUN_ID, tmp_path, [dict(r) for r in rows])

    original_head = original_events[-1]["event_hash"]

    body = {
        "schema_version": 1,
        "run_id": _RUN_ID,
        "original_journal_head": original_head,
        "substituted_journal_head": original_head,
        "first_divergent_step": None,
        "substitution_label": "test-substitution",
        "verdict": EquivalenceVerdict.EQUIVALENT.value,
        "timestamp": _TS,
    }
    attestation_hash = _hash_obj(body)

    attestation = EquivalenceAttestation(
        schema_version=1,
        run_id=_RUN_ID,
        original_journal_head=original_head,
        substituted_journal_head=original_head,
        first_divergent_step=None,
        substitution_label="test-substitution",
        verdict=EquivalenceVerdict.EQUIVALENT.value,
        timestamp=_TS,
        attestation_hash=attestation_hash,
    )

    _write_attestation(tmp_path, attestation)

    result = verify_equivalence_attestation(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        attestation_hash=attestation_hash,
    )

    assert result.ok is False
    assert "substituted run journal unavailable" in result.reason


def test_verify_equivalence_attestation_substituted_journal_chain_broken(tmp_path: Path) -> None:
    """Substituted journal chain broken -> ok=False reason mentions substituted run journal chain broken."""
    rows = _clean_rows()
    original_events = _seed_journal(_ORIGINAL_RUN_ID, tmp_path, [dict(r) for r in rows])
    substituted_events = _seed_journal(_RUN_ID, tmp_path, [dict(r) for r in rows])
    substituted_events[1]["event_hash"] = "a" * 64

    original_head = original_events[-1]["event_hash"]
    substituted_head = substituted_events[-1]["event_hash"]

    body = {
        "schema_version": 1,
        "run_id": _RUN_ID,
        "original_journal_head": original_head,
        "substituted_journal_head": substituted_head,
        "first_divergent_step": 1,
        "substitution_label": "test-substitution",
        "verdict": EquivalenceVerdict.DIVERGED.value,
        "timestamp": _TS,
    }
    attestation_hash = _hash_obj(body)

    attestation = EquivalenceAttestation(
        schema_version=1,
        run_id=_RUN_ID,
        original_journal_head=original_head,
        substituted_journal_head=substituted_head,
        first_divergent_step=1,
        substitution_label="test-substitution",
        verdict=EquivalenceVerdict.DIVERGED.value,
        timestamp=_TS,
        attestation_hash=attestation_hash,
    )

    _write_attestation(tmp_path, attestation)

    result = verify_equivalence_attestation(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        attestation_hash=attestation_hash,
        original_journal_events=original_events,
        substituted_journal_events=substituted_events,
    )

    assert result.ok is False
    assert "substituted run journal chain broken" in result.reason


def test_verify_equivalence_attestation_substituted_head_mismatch(tmp_path: Path) -> None:
    """Substituted journal head mismatch -> ok=False reason mentions substituted_journal_head."""
    rows = _clean_rows()
    original_events = _seed_journal(_ORIGINAL_RUN_ID, tmp_path, [dict(r) for r in rows])
    substituted_events = _seed_journal(_RUN_ID, tmp_path, [dict(r) for r in rows])

    original_head = original_events[-1]["event_hash"]

    body = {
        "schema_version": 1,
        "run_id": _RUN_ID,
        "original_journal_head": original_head,
        "substituted_journal_head": "c" * 64,
        "first_divergent_step": 1,
        "substitution_label": "test-substitution",
        "verdict": EquivalenceVerdict.DIVERGED.value,
        "timestamp": _TS,
    }
    attestation_hash = _hash_obj(body)

    attestation = EquivalenceAttestation(
        schema_version=1,
        run_id=_RUN_ID,
        original_journal_head=original_head,
        substituted_journal_head="c" * 64,
        first_divergent_step=1,
        substitution_label="test-substitution",
        verdict=EquivalenceVerdict.DIVERGED.value,
        timestamp=_TS,
        attestation_hash=attestation_hash,
    )

    _write_attestation(tmp_path, attestation)

    result = verify_equivalence_attestation(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        attestation_hash=attestation_hash,
        original_journal_events=original_events,
        substituted_journal_events=substituted_events,
    )

    assert result.ok is False
    assert "substituted_journal_head" in result.reason


def test_verify_equivalence_attestation_verdict_mismatch(tmp_path: Path) -> None:
    """Verdict mismatch (stored DIVERGED but heads match / first_divergent_step None) -> ok=False."""
    rows = _clean_rows()
    original_events = _seed_journal(_ORIGINAL_RUN_ID, tmp_path, [dict(r) for r in rows])
    substituted_events = _seed_journal(_RUN_ID, tmp_path, [dict(r) for r in rows])

    original_head = original_events[-1]["event_hash"]
    substituted_head = substituted_events[-1]["event_hash"]

    assert original_head == substituted_head

    body = {
        "schema_version": 1,
        "run_id": _RUN_ID,
        "original_journal_head": original_head,
        "substituted_journal_head": substituted_head,
        "first_divergent_step": None,
        "substitution_label": "test-substitution",
        "verdict": EquivalenceVerdict.DIVERGED.value,
        "timestamp": _TS,
    }
    attestation_hash = _hash_obj(body)

    attestation = EquivalenceAttestation(
        schema_version=1,
        run_id=_RUN_ID,
        original_journal_head=original_head,
        substituted_journal_head=substituted_head,
        first_divergent_step=None,
        substitution_label="test-substitution",
        verdict=EquivalenceVerdict.DIVERGED.value,
        timestamp=_TS,
        attestation_hash=attestation_hash,
    )

    _write_attestation(tmp_path, attestation)
    _anchor_in_spine(tmp_path, attestation)

    result = verify_equivalence_attestation(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        attestation_hash=attestation_hash,
        original_journal_events=original_events,
        substituted_journal_events=substituted_events,
    )

    assert result.ok is False
    assert "equivalence verdict mismatch" in result.reason


def test_verify_equivalence_attestation_not_anchored_in_spine(tmp_path: Path) -> None:
    """Not anchored in spine -> ok=False reason mentions not anchored."""
    rows = _clean_rows()
    original_events = _seed_journal(_ORIGINAL_RUN_ID, tmp_path, [dict(r) for r in rows])
    substituted_events = _seed_journal(_RUN_ID, tmp_path, [dict(r) for r in rows])

    original_head = original_events[-1]["event_hash"]
    substituted_head = substituted_events[-1]["event_hash"]

    assert original_head == substituted_head

    body = {
        "schema_version": 1,
        "run_id": _RUN_ID,
        "original_journal_head": original_head,
        "substituted_journal_head": substituted_head,
        "first_divergent_step": None,
        "substitution_label": "test-substitution",
        "verdict": EquivalenceVerdict.EQUIVALENT.value,
        "timestamp": _TS,
    }
    attestation_hash = _hash_obj(body)

    attestation = EquivalenceAttestation(
        schema_version=1,
        run_id=_RUN_ID,
        original_journal_head=original_head,
        substituted_journal_head=substituted_head,
        first_divergent_step=None,
        substitution_label="test-substitution",
        verdict=EquivalenceVerdict.EQUIVALENT.value,
        timestamp=_TS,
        attestation_hash=attestation_hash,
    )

    _write_attestation(tmp_path, attestation)

    result = verify_equivalence_attestation(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        attestation_hash=attestation_hash,
        original_journal_events=original_events,
        substituted_journal_events=substituted_events,
    )

    assert result.ok is False
    assert "not anchored" in result.reason


def test_a_crafted_run_id_cannot_address_a_journal_outside_the_runs_root(tmp_path: Path) -> None:
    """A run id that walks out of the runs root is refused, not followed.

    The run id arrives inside the attestation being audited, so it is
    attacker-reachable input to a path. Reading it as a path segment let a
    crafted id point the "original journal" at any journal on disk, and the
    replay would then compare against that one.
    """
    import dataclasses

    from bernstein.eval.clean_run import CounterfactualAuditRefusal, build_equivalence_attestation
    from tests.unit.test_eval_clean_run import _build

    # A real Merkle-chained journal: _build refuses events that do not chain,
    # so the raw row dicts are not enough to reach the code under test.
    chained = _seed_journal("run-clean-1", tmp_path, _clean_rows())
    original = _build(tmp_path, chained)

    # A journal that exists, and that the crafted id resolves onto if the
    # path is built by concatenation instead of containment.
    outside = tmp_path / "elsewhere"
    _seed_journal("planted", outside, _clean_rows())

    escaped = dataclasses.replace(original, run_id="../../elsewhere/.sdd/runs/planted")

    # Catch broadly on purpose: without containment the planted journal is
    # found, accepted, and the replay proceeds until it dies of something
    # unrelated. That failure is the escape being followed, so the assertion
    # names it rather than letting an opaque traceback stand in for it.
    try:
        build_equivalence_attestation(
            original_attestation=escaped,
            workdir=tmp_path,
            lineage_root=tmp_path / ".sdd" / "lineage",
            hmac_key=_KEY,
            substitution_content={"src/mathlib.py": "def add(a, b): return a * b"},
            timestamp=_TS,
        )
    except Exception as exc:
        assert isinstance(exc, CounterfactualAuditRefusal), (
            f"the escaping run id was followed instead of refused; it got as far as {exc!r}"
        )
        assert "not addressable" in str(exc), f"refused, but for the wrong reason: {exc}"
    else:
        raise AssertionError("a run id escaping the runs root was accepted")
