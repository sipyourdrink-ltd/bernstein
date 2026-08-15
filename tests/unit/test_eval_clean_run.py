"""Tests for the clean-run attestation behind eval scoring (#2930).

A clean-run attestation binds a task's ground-truth (as a keyed commitment,
never plaintext) to the run's complete journaled activity and proves the two
never intersected and that no read escaped the task worktree. Offline
verification re-derives the verdict from the embedded evidence, so an
attestation whose stored verdict its evidence does not entail is rejected even
when its hashes are internally consistent, and an activity set that does not
chain to the recorded journal head is rejected as unanchored.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from bernstein.core.replay.journal import EventJournal, load_events
from bernstein.core.security.network_isolation import Endpoint, NetworkPolicy
from bernstein.eval.clean_run import (
    CleanRunBoundaryError,
    CleanRunVerdict,
    build_clean_run_attestation,
    build_contraband_set,
    clean_run_attestation_path,
    extract_activity,
    recompute_attestation_hash,
    scan_activity,
    scope_boundary,
    verify_clean_run_attestation,
)
from bernstein.eval.golden import GoldenTask

_KEY = b"k" * 32
_TS = 1_700_000_000

_REFERENCE_SOLUTION = (
    "def fibonacci(n):\n    if n < 2:\n        return n\n    return fibonacci(n - 1) + fibonacci(n - 2)\n"
)

_HIDDEN_TEST_TOKEN = "expect-fib-10-equals-55-sentinel"


def _task() -> GoldenTask:
    return GoldenTask(
        id="golden-fib-001",
        tier="smoke",
        title="Implement fibonacci helper",
        description="Add a fibonacci helper to the math module.",
        completion_signals=[_HIDDEN_TEST_TOKEN],
        expected_test_outcomes={"pytest tests/test_fib.py": True},
    )


def _reference_blobs() -> dict[str, str]:
    return {"reference/solution.py": _REFERENCE_SOLUTION}


def _plaintexts() -> list[str]:
    """Every ground-truth plaintext the attestation must never carry."""
    return [
        "golden-fib-001",
        "Implement fibonacci helper",
        _HIDDEN_TEST_TOKEN,
        "pytest tests/test_fib.py",
        "fibonacci(n - 1) + fibonacci(n - 2)",
    ]


def _worktree(tmp_path: Path) -> Path:
    root = tmp_path / "wt"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _policy() -> NetworkPolicy:
    return NetworkPolicy(allowed_endpoints=(Endpoint("127.0.0.1", 8052),))


def _seed_journal(tmp_path: Path, rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """Record *rows* into a real Merkle-chained journal and load them back."""
    journal = EventJournal("run-clean-1", tmp_path / ".sdd")
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


def _build(
    tmp_path: Path,
    journal_events: list[dict[str, object]],
    *,
    chain: object | None = None,
):
    return build_clean_run_attestation(
        task=_task(),
        reference_blobs=_reference_blobs(),
        journal_events=journal_events,
        run_id="run-clean-1",
        worktree_root=_worktree(tmp_path),
        network_policy=_policy(),
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        timestamp=_TS,
        chain=chain,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# 1. Contraband commitment is leak-proof and byte-stable
# ---------------------------------------------------------------------------


def test_contraband_set_contains_no_plaintext_ground_truth() -> None:
    contraband = build_contraband_set(_task(), key=_KEY, reference_blobs=_reference_blobs())
    blob = json.dumps(contraband.to_dict(), ensure_ascii=False)
    for plaintext in _plaintexts():
        assert plaintext not in blob
        assert plaintext.lower() not in blob.lower()


def test_contraband_set_is_byte_stable_across_builds() -> None:
    a = build_contraband_set(_task(), key=_KEY, reference_blobs=_reference_blobs())
    b = build_contraband_set(_task(), key=_KEY, reference_blobs=dict(reversed(list(_reference_blobs().items()))))
    assert a.canonical_bytes() == b.canonical_bytes()


def test_contraband_set_differs_under_a_different_key() -> None:
    a = build_contraband_set(_task(), key=_KEY, reference_blobs=_reference_blobs())
    b = build_contraband_set(_task(), key=b"j" * 32, reference_blobs=_reference_blobs())
    assert a.canonical_bytes() != b.canonical_bytes()


_GOLDEN_FRONTMATTER = (
    "id: golden-fib-001\n"
    "title: Implement fibonacci helper\n"
    "completion_signals:\n"
    f'  - "{_HIDDEN_TEST_TOKEN}"\n'
    "expected_test_outcomes:\n"
    '  "pytest tests/test_fib.py": true\n'
)


def _write_golden_source(golden_dir: Path) -> None:
    tier_dir = golden_dir / "smoke"
    tier_dir.mkdir(parents=True, exist_ok=True)
    (tier_dir / "golden-fib-001.md").write_text(
        f"---\n{_GOLDEN_FRONTMATTER}---\n\nAdd a fibonacci helper to the math module.\n",
        encoding="utf-8",
    )


def test_the_commitment_cannot_silently_omit_a_tasks_reference_solution(tmp_path: Path) -> None:
    """Omitting the reference_blobs param must not weaken the commitment.

    The task's own golden-source frontmatter is derived by the builder, so a
    read of that hidden material still flips DIRTY even when the caller
    supplied no reference blobs at all.
    """
    golden_dir = tmp_path / "golden"
    _write_golden_source(golden_dir)
    rows = _clean_rows()
    rows.append({"event": "file_read", "path": "src/notes.py", "content_window": _GOLDEN_FRONTMATTER.strip()})
    events = _seed_journal(tmp_path, rows)
    attestation = build_clean_run_attestation(
        task=_task(),
        journal_events=events,
        run_id="run-clean-1",
        worktree_root=_worktree(tmp_path),
        network_policy=_policy(),
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        timestamp=_TS,
        golden_dir=golden_dir,
    )
    assert attestation.verdict == CleanRunVerdict.DIRTY.value
    assert any(m.match_class == "contraband_ngram" for m in attestation.matches)
    assert attestation.contraband.reference_source_count == 1


def test_commitment_coverage_is_sealed_and_visible() -> None:
    """Coverage counts sit inside the signed bytes -- absence is loud."""
    with_reference = build_contraband_set(_task(), key=_KEY, reference_blobs=_reference_blobs())
    assert with_reference.reference_source_count == 1
    assert with_reference.token_source_count >= 3
    assert with_reference.to_dict()["reference_source_count"] == 1

    without_reference = build_contraband_set(_task(), key=_KEY)
    assert without_reference.reference_source_count == 0
    assert without_reference.to_dict()["reference_source_count"] == 0


def test_extra_reference_material_cannot_displace_the_derived_ground_truth(tmp_path: Path) -> None:
    """Additive means strictly additive: a label collision refuses to seal.

    A caller-supplied blob whose key collides with the derived golden-source
    label must not displace the task's own ground-truth from the commitment;
    the seal refuses naming the colliding key. Non-colliding extra material
    still merges additively.
    """
    from bernstein.eval.clean_run import CleanRunCommitmentError

    golden_dir = tmp_path / "golden"
    _write_golden_source(golden_dir)
    events = _seed_journal(tmp_path, _clean_rows())
    with pytest.raises(CleanRunCommitmentError) as excinfo:
        build_clean_run_attestation(
            task=_task(),
            journal_events=events,
            run_id="run-clean-1",
            worktree_root=_worktree(tmp_path),
            network_policy=_policy(),
            workdir=tmp_path,
            lineage_root=tmp_path / ".sdd" / "lineage",
            hmac_key=_KEY,
            timestamp=_TS,
            golden_dir=golden_dir,
            reference_blobs={"golden:golden-fib-001": "innocuous decoy"},
        )
    assert "golden:golden-fib-001" in str(excinfo.value)

    attestation = build_clean_run_attestation(
        task=_task(),
        journal_events=events,
        run_id="run-clean-1",
        worktree_root=_worktree(tmp_path),
        network_policy=_policy(),
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        timestamp=_TS,
        golden_dir=golden_dir,
        reference_blobs=_reference_blobs(),
    )
    assert attestation.contraband.reference_source_count == 2


def test_builder_refuses_a_declared_golden_dir_without_the_tasks_source(tmp_path: Path) -> None:
    """Declared reference content that cannot be loaded refuses to seal."""
    from bernstein.eval.clean_run import CleanRunCommitmentError

    golden_dir = tmp_path / "golden"
    (golden_dir / "smoke").mkdir(parents=True)
    events = _seed_journal(tmp_path, _clean_rows())
    with pytest.raises(CleanRunCommitmentError):
        build_clean_run_attestation(
            task=_task(),
            journal_events=events,
            run_id="run-clean-1",
            worktree_root=_worktree(tmp_path),
            network_policy=_policy(),
            workdir=tmp_path,
            lineage_root=tmp_path / ".sdd" / "lineage",
            hmac_key=_KEY,
            timestamp=_TS,
            golden_dir=golden_dir,
        )
    assert not (tmp_path / ".sdd" / "eval" / "clean_run").exists()


# ---------------------------------------------------------------------------
# 2. Scan detects a planted contaminating read and an out-of-scope path
# ---------------------------------------------------------------------------


def test_planted_contaminating_read_flips_verdict_to_dirty(tmp_path: Path) -> None:
    rows = _clean_rows()
    rows.insert(1, {"event": "file_read", "path": "src/notes.py", "content_window": _REFERENCE_SOLUTION})
    events = _seed_journal(tmp_path, rows)
    attestation = _build(tmp_path, events)
    assert attestation.verdict == CleanRunVerdict.DIRTY.value
    assert any(m.index == 1 and m.match_class == "contraband_ngram" for m in attestation.matches)


def test_completion_signal_token_in_tool_args_flips_verdict_to_dirty(tmp_path: Path) -> None:
    rows = _clean_rows()
    rows.append({"event": "tool_call", "arguments": {"query": _HIDDEN_TEST_TOKEN}})
    events = _seed_journal(tmp_path, rows)
    attestation = _build(tmp_path, events)
    assert attestation.verdict == CleanRunVerdict.DIRTY.value
    assert any(m.match_class == "contraband_token" for m in attestation.matches)


def test_out_of_scope_path_read_flips_verdict_to_dirty(tmp_path: Path) -> None:
    rows = _clean_rows()
    rows.append({"event": "file_read", "path": "../../sibling-worktree/answer.py"})
    events = _seed_journal(tmp_path, rows)
    attestation = _build(tmp_path, events)
    assert attestation.verdict == CleanRunVerdict.DIRTY.value
    assert any(m.match_class == "out_of_scope_path" for m in attestation.matches)


def test_out_of_allowlist_egress_flips_verdict_to_dirty(tmp_path: Path) -> None:
    rows = _clean_rows()
    rows.append({"event": "network_egress", "endpoint": "203.0.113.9:443"})
    events = _seed_journal(tmp_path, rows)
    attestation = _build(tmp_path, events)
    assert attestation.verdict == CleanRunVerdict.DIRTY.value
    assert any(m.match_class == "out_of_scope_endpoint" for m in attestation.matches)


def test_clean_run_scans_clean(tmp_path: Path) -> None:
    events = _seed_journal(tmp_path, _clean_rows())
    attestation = _build(tmp_path, events)
    assert attestation.verdict == CleanRunVerdict.CLEAN.value
    assert attestation.matches == ()


def test_attestation_artifact_carries_no_plaintext_ground_truth(tmp_path: Path) -> None:
    rows = _clean_rows()
    rows.insert(0, {"event": "file_read", "path": "src/notes.py", "content_window": _REFERENCE_SOLUTION})
    events = _seed_journal(tmp_path, rows)
    attestation = _build(tmp_path, events)
    blob = attestation.canonical_bytes().decode("utf-8")
    for plaintext in _plaintexts():
        assert plaintext not in blob


# ---------------------------------------------------------------------------
# 3. Activity set is head-anchored (journal head only; mirror chain-checked)
# ---------------------------------------------------------------------------


def test_attestation_binds_the_journal_head_only_and_mirror_is_chain_checked(tmp_path: Path) -> None:
    """The sealed contract carries one anchor: the journal head.

    The audit-chain mirror carries the attestation's own hash, so it cannot
    sit inside the sealed bytes; its integrity is checked by audit verify
    (and its coverage by the receipt projection), never by a sealed field
    that verification would have to take on faith.
    """
    from bernstein.core.security.audit_chain import EVENT_CLEAN_RUN_ATTESTATION, AuditChainStore

    events = _seed_journal(tmp_path, _clean_rows())
    chain = AuditChainStore(tmp_path / "audit", key=_KEY)
    attestation = _build(tmp_path, events, chain=chain)
    assert attestation.journal_head == str(events[-1]["event_hash"])
    assert "chain_range_head" not in attestation.to_dict()
    mirrors = chain.query(event_type=EVENT_CLEAN_RUN_ATTESTATION)
    assert [e.details["attestation_hash"] for e in mirrors] == [attestation.attestation_hash]
    ok, errors = chain.verify()
    assert ok, errors


def test_mutating_a_scanned_event_changes_the_anchor(tmp_path: Path) -> None:
    events = _seed_journal(tmp_path, _clean_rows())
    attestation = _build(tmp_path, events)
    mutated = [dict(e) for e in events]
    mutated[0]["path"] = "src/other.py"
    result = verify_clean_run_attestation(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        attestation_hash=attestation.attestation_hash,
        journal_events=mutated,
    )
    assert not result.ok
    assert "unanchored" in result.reason


def test_dropping_a_scanned_event_is_rejected_as_unanchored(tmp_path: Path) -> None:
    events = _seed_journal(tmp_path, _clean_rows())
    attestation = _build(tmp_path, events)
    result = verify_clean_run_attestation(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        attestation_hash=attestation.attestation_hash,
        journal_events=events[:-1],
    )
    assert not result.ok
    assert "unanchored" in result.reason


# ---------------------------------------------------------------------------
# 4. Seal + mirror (spine anchor, audit-chain event, determinism)
# ---------------------------------------------------------------------------


def test_attestation_is_anchored_in_the_spine_and_binding_is_deterministic(tmp_path: Path) -> None:
    from bernstein.core.lineage.spine import LineageSpine, content_hash_of
    from bernstein.eval.clean_run import EVAL_CLEAN_RUN_RUN_ID

    worktree = _worktree(tmp_path)
    events_a = _seed_journal(tmp_path / "a", _clean_rows())
    events_b = _seed_journal(tmp_path / "b", _clean_rows())

    def _build_at(workdir: Path, events: list[dict[str, object]]):
        return build_clean_run_attestation(
            task=_task(),
            reference_blobs=_reference_blobs(),
            journal_events=events,
            run_id="run-clean-1",
            worktree_root=worktree,
            network_policy=_policy(),
            workdir=workdir,
            lineage_root=workdir / ".sdd" / "lineage",
            hmac_key=_KEY,
            timestamp=_TS,
        )

    a = _build_at(tmp_path / "a", events_a)
    b = _build_at(tmp_path / "b", events_b)
    assert a.canonical_bytes() == b.canonical_bytes()
    assert a.attestation_hash == b.attestation_hash
    assert a.journal_entry_hash == b.journal_entry_hash

    spine = LineageSpine(tmp_path / "a" / ".sdd" / "lineage", run_id=EVAL_CLEAN_RUN_RUN_ID, hmac_key=_KEY)
    entries = list(spine.iter_entries())
    assert any(
        e.entry_hash == a.journal_entry_hash and e.content_hash == content_hash_of(a.canonical_bytes()) for e in entries
    )


def test_attestation_mirrors_into_audit_chain_hashes_only(tmp_path: Path) -> None:
    from bernstein.core.security.audit_chain import EVENT_CLEAN_RUN_ATTESTATION, AuditChainStore

    events = _seed_journal(tmp_path, _clean_rows())
    chain = AuditChainStore(tmp_path / "audit", key=_KEY)
    attestation = _build(tmp_path, events, chain=chain)
    rows = chain.query(event_type=EVENT_CLEAN_RUN_ATTESTATION)
    assert len(rows) == 1
    details = rows[0].details
    assert details["attestation_hash"] == attestation.attestation_hash
    assert details["verdict"] == attestation.verdict
    assert details["journal_head"] == attestation.journal_head
    assert details["journal_entry_hash"] == attestation.journal_entry_hash
    assert "prev_chain_digest" in details
    blob = repr(details)
    for plaintext in _plaintexts():
        assert plaintext not in blob
    ok, errors = chain.verify()
    assert ok, errors


def test_single_byte_tamper_of_mirrored_event_fails_audit_verify(tmp_path: Path) -> None:
    from bernstein.core.security.audit_chain import AuditChainStore

    events = _seed_journal(tmp_path, _clean_rows())
    chain = AuditChainStore(tmp_path / "audit", key=_KEY)
    _build(tmp_path, events, chain=chain)
    log_files = sorted((tmp_path / "audit").glob("*.jsonl"))
    assert log_files
    raw = log_files[-1].read_text(encoding="utf-8")
    assert "clean_run_attestation" in raw
    log_files[-1].write_text(raw.replace(CleanRunVerdict.CLEAN.value, "cxean", 1), encoding="utf-8")
    ok, errors = AuditChainStore(tmp_path / "audit", key=_KEY).verify()
    assert not ok
    assert errors


# ---------------------------------------------------------------------------
# 5. verify re-derives the verdict from the embedded evidence
# ---------------------------------------------------------------------------


def test_verify_accepts_an_intact_attestation(tmp_path: Path) -> None:
    events = _seed_journal(tmp_path, _clean_rows())
    attestation = _build(tmp_path, events)
    result = verify_clean_run_attestation(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        attestation_hash=attestation.attestation_hash,
        journal_events=events,
    )
    assert result.ok, result.reason


def test_stored_clean_with_embedded_match_is_rejected(tmp_path: Path) -> None:
    """A forged CLEAN verdict over dirty embedded evidence is rejected.

    The forged attestation recomputes its own hash, so every hash is
    internally consistent; only re-derivation of the verdict from the
    embedded activity digests and contraband commitment can catch it.
    """
    rows = _clean_rows()
    rows.append({"event": "tool_call", "arguments": {"query": _HIDDEN_TEST_TOKEN}})
    events = _seed_journal(tmp_path, rows)
    attestation = _build(tmp_path, events)
    assert attestation.verdict == CleanRunVerdict.DIRTY.value

    path = clean_run_attestation_path(tmp_path, attestation.attestation_hash)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["verdict"] = CleanRunVerdict.CLEAN.value
    payload["matches"] = []
    payload["attestation_hash"] = recompute_attestation_hash(payload)
    forged_path = clean_run_attestation_path(tmp_path, payload["attestation_hash"])
    forged_path.write_text(json.dumps(payload), encoding="utf-8")
    result = verify_clean_run_attestation(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        attestation_hash=payload["attestation_hash"],
        journal_events=events,
    )
    assert not result.ok
    assert "entail" in result.reason


def test_trimmed_embedded_activity_is_rejected(tmp_path: Path) -> None:
    """Dropping the contaminating activity record from the embedded set fails.

    The journal still chains, so the anchor holds; the re-derivation of the
    sealed activity set from the anchored journal is what catches the trim.
    """
    rows = _clean_rows()
    rows.append({"event": "tool_call", "arguments": {"query": _HIDDEN_TEST_TOKEN}})
    events = _seed_journal(tmp_path, rows)
    attestation = _build(tmp_path, events)

    path = clean_run_attestation_path(tmp_path, attestation.attestation_hash)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["activities"] = payload["activities"][:-1]
    payload["verdict"] = CleanRunVerdict.CLEAN.value
    payload["matches"] = []
    payload["attestation_hash"] = recompute_attestation_hash(payload)
    forged_path = clean_run_attestation_path(tmp_path, payload["attestation_hash"])
    forged_path.write_text(json.dumps(payload), encoding="utf-8")
    result = verify_clean_run_attestation(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        attestation_hash=payload["attestation_hash"],
        journal_events=events,
    )
    assert not result.ok


def test_a_type_altered_stored_attestation_cannot_verify_as_the_signed_body(tmp_path: Path) -> None:
    """Alternate JSON spellings of the signed body must not re-verify.

    Coercing deserializers would let ``"1700000000"`` (str) or ``true``
    (bool, an int subclass) re-canonicalize to the exact body hash that was
    signed over an int -- distinct stored bytes verifying as one signed
    body. Exact-type parsing rejects both as schema-invalid.
    """
    events = _seed_journal(tmp_path, _clean_rows())
    attestation = _build(tmp_path, events)
    path = clean_run_attestation_path(tmp_path, attestation.attestation_hash)
    original = json.loads(path.read_text(encoding="utf-8"))
    for field, altered in (("timestamp", str(original["timestamp"])), ("schema_version", True)):
        payload = dict(original)
        payload[field] = altered
        path.write_text(json.dumps(payload), encoding="utf-8")
        result = verify_clean_run_attestation(
            workdir=tmp_path,
            lineage_root=tmp_path / ".sdd" / "lineage",
            hmac_key=_KEY,
            attestation_hash=attestation.attestation_hash,
            journal_events=events,
        )
        assert not result.ok, f"type-altered {field} must not verify"
        assert "schema-invalid" in result.reason, f"{field}: {result.reason}"


def test_tampering_any_stored_field_breaks_verification(tmp_path: Path) -> None:
    events = _seed_journal(tmp_path, _clean_rows())
    attestation = _build(tmp_path, events)
    path = clean_run_attestation_path(tmp_path, attestation.attestation_hash)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["journal_head"] = "0" * 64
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = verify_clean_run_attestation(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        attestation_hash=attestation.attestation_hash,
        journal_events=events,
    )
    assert not result.ok


# ---------------------------------------------------------------------------
# 6. No worktree boundary -> refuse to sign
# ---------------------------------------------------------------------------


def test_builder_refuses_to_sign_without_a_worktree_boundary(tmp_path: Path) -> None:
    events = _seed_journal(tmp_path, _clean_rows())
    with pytest.raises(CleanRunBoundaryError):
        build_clean_run_attestation(
            task=_task(),
            reference_blobs=_reference_blobs(),
            journal_events=events,
            run_id="run-clean-1",
            worktree_root=None,
            network_policy=_policy(),
            workdir=tmp_path,
            lineage_root=tmp_path / ".sdd" / "lineage",
            hmac_key=_KEY,
            timestamp=_TS,
        )
    assert not (tmp_path / ".sdd" / "eval" / "clean_run").exists()


def test_builder_refuses_a_missing_worktree_directory(tmp_path: Path) -> None:
    events = _seed_journal(tmp_path, _clean_rows())
    with pytest.raises(CleanRunBoundaryError):
        build_clean_run_attestation(
            task=_task(),
            reference_blobs=_reference_blobs(),
            journal_events=events,
            run_id="run-clean-1",
            worktree_root=tmp_path / "does-not-exist",
            network_policy=_policy(),
            workdir=tmp_path,
            lineage_root=tmp_path / ".sdd" / "lineage",
            hmac_key=_KEY,
            timestamp=_TS,
        )


def test_builder_refuses_an_empty_journal(tmp_path: Path) -> None:
    from bernstein.eval.clean_run import CleanRunAnchorError

    with pytest.raises(CleanRunAnchorError):
        _build(tmp_path, [])


# ---------------------------------------------------------------------------
# 7. Offline receipt projection over the mirrored chain range
# ---------------------------------------------------------------------------


def test_receipt_projection_subject_digest_covers_the_attestation(tmp_path: Path) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from bernstein.core.security.audit_chain import AuditChainStore
    from bernstein.core.security.lineage_kms import FileBasedKMSAdapter
    from bernstein.eval.clean_run import project_clean_run_receipt

    events = _seed_journal(tmp_path, _clean_rows())
    chain = AuditChainStore(tmp_path / "audit", key=_KEY)
    attestation = _build(tmp_path, events, chain=chain)

    key = Ed25519PrivateKey.from_private_bytes(b"i" * 32)
    key_path = tmp_path / "sign.pem"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )
    kms = FileBasedKMSAdapter(key_path, kid="clean-run-test-key")

    receipt = project_clean_run_receipt(
        tmp_path / "audit",
        attestation_hash=attestation.attestation_hash,
        since="2000-01-01T00:00:00.000000Z",
        until="2100-01-01T00:00:00.000000Z",
        key=_KEY,
        kms_adapter=kms,
        write=False,
    )
    assert receipt.formats == ("cose", "intoto", "transparency")
    assert receipt.receipt["subject"]["digest"]["sha256"] == receipt.head_sha256
    assert any(
        e.get("details", {}).get("attestation_hash") == attestation.attestation_hash for e in receipt.receipt["events"]
    )
    blob = receipt.receipt_bytes.decode("utf-8")
    for plaintext in _plaintexts():
        assert plaintext not in blob


def test_receipt_projection_refuses_a_range_without_the_attestation(tmp_path: Path) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from bernstein.core.security.audit_chain import AuditChainStore
    from bernstein.core.security.lineage_kms import FileBasedKMSAdapter
    from bernstein.eval.clean_run import CleanRunProjectionError, project_clean_run_receipt

    events = _seed_journal(tmp_path, _clean_rows())
    chain = AuditChainStore(tmp_path / "audit", key=_KEY)
    _build(tmp_path, events, chain=chain)

    key = Ed25519PrivateKey.from_private_bytes(b"i" * 32)
    key_path = tmp_path / "sign.pem"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )
    with pytest.raises(CleanRunProjectionError):
        project_clean_run_receipt(
            tmp_path / "audit",
            attestation_hash="sha256:" + "f" * 64,
            since="2000-01-01T00:00:00.000000Z",
            until="2100-01-01T00:00:00.000000Z",
            key=_KEY,
            kms_adapter=FileBasedKMSAdapter(key_path, kid="clean-run-test-key"),
            write=False,
        )


# ---------------------------------------------------------------------------
# Store hardening: a linked store component is refused, never followed
# ---------------------------------------------------------------------------


def test_a_symlinked_clean_run_store_is_refused_not_followed(tmp_path: Path) -> None:
    # A genuine attestation, sealed elsewhere, planted in an outside
    # directory that a symlinked clean-run store points at.
    # realpath-vs-realpath containment passes vacuously in that layout, so
    # the store must refuse the symlink itself.
    from bernstein.eval.clean_run import read_clean_run_attestation

    elsewhere = tmp_path / "elsewhere"
    events = _seed_journal(elsewhere, _clean_rows())
    attestation = _build(elsewhere, events)
    attestation_json = clean_run_attestation_path(elsewhere, attestation.attestation_hash).read_text(encoding="utf-8")

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / f"{attestation.attestation_hash}.json").write_text(attestation_json, encoding="utf-8")

    victim = tmp_path / "victim"
    (victim / ".sdd" / "eval").mkdir(parents=True)
    (victim / ".sdd" / "eval" / "clean_run").symlink_to(outside)

    assert read_clean_run_attestation(victim, attestation.attestation_hash) is None
    with pytest.raises(ValueError, match="symlink"):
        clean_run_attestation_path(victim, attestation.attestation_hash)
    result = verify_clean_run_attestation(
        workdir=victim,
        lineage_root=victim / ".sdd" / "lineage",
        hmac_key=_KEY,
        attestation_hash=attestation.attestation_hash,
        journal_events=events,
    )
    assert not result.ok
    assert "symlink" in result.reason

    # The CLI surfaces the refusal as a nonzero exit, never a traceback.
    import os as _os

    from click.testing import CliRunner

    from bernstein.cli.commands.eval_benchmark_cmd import eval_group

    key_file = tmp_path / "audit.key"
    key_file.write_bytes(_KEY)
    _os.chmod(key_file, 0o600)
    cli_result = CliRunner().invoke(
        eval_group,
        ["clean-run", "verify", attestation.attestation_hash, "--workdir", str(victim)],
        env={"BERNSTEIN_AUDIT_KEY_PATH": str(key_file)},
    )
    assert cli_result.exit_code == 1
    assert "Traceback" not in cli_result.output


class _JunctionProbePath(Path):
    """POSIX stand-in for an NTFS junction on the store walk.

    ``is_junction()`` answers ``True`` for the marked component while
    ``is_symlink()`` keeps its real (``False``) answer, so
    ``is_filesystem_link`` takes its junction branch through the real
    component walk rather than having the whole check stubbed away.
    """

    _junction_component = "clean_run"

    def is_junction(self) -> bool:
        return self.name == self._junction_component


def test_a_junction_store_component_is_refused_like_a_symlink(tmp_path: Path) -> None:
    # Path.is_symlink() is False for NTFS junctions, so a symlink-only probe
    # is bypassed by a junctioned store component on Windows. A component
    # that is a filesystem link of any kind must be refused before the
    # realpath containment check, with no candidate returned.
    from bernstein.eval.clean_run import read_clean_run_attestation

    events = _seed_journal(tmp_path, _clean_rows())
    attestation = _build(tmp_path, events)
    workdir = _JunctionProbePath(tmp_path)

    with pytest.raises(ValueError, match="symlink or junction"):
        clean_run_attestation_path(workdir, attestation.attestation_hash)
    assert read_clean_run_attestation(workdir, attestation.attestation_hash) is None


class _UnprobeableProbePath(Path):
    """Store walk stand-in whose link probe itself fails.

    ``is_symlink`` raises ``PermissionError`` for the marked component, so
    the fail-closed caller-side probe sees a genuine probe failure through
    the real walk rather than a stubbed helper.
    """

    _unprobeable_component = "clean_run"

    def is_symlink(self) -> bool:
        if self.name == self._unprobeable_component:
            raise PermissionError(13, "Permission denied")
        return super().is_symlink()


def test_an_unprobeable_store_component_is_refused_by_name(tmp_path: Path) -> None:
    """A component that cannot be probed for links refuses, never continues.

    The shared best-effort helper answers False on a probe error; a store
    walk that cannot prove a component is not a link must fail closed.
    """
    from bernstein.eval.clean_run import read_clean_run_attestation

    events = _seed_journal(tmp_path, _clean_rows())
    attestation = _build(tmp_path, events)
    workdir = _UnprobeableProbePath(tmp_path)

    with pytest.raises(ValueError, match="could not be probed for links"):
        clean_run_attestation_path(workdir, attestation.attestation_hash)
    assert read_clean_run_attestation(workdir, attestation.attestation_hash) is None


def test_sealing_into_a_fresh_workdir_still_creates_the_store(tmp_path: Path) -> None:
    """Nonexistent store components are not probe failures: the seal creates them."""
    events = _seed_journal(tmp_path / "j", _clean_rows())
    fresh = tmp_path / "fresh"
    worktree = tmp_path / "wt"
    worktree.mkdir()
    attestation = build_clean_run_attestation(
        task=_task(),
        reference_blobs=_reference_blobs(),
        journal_events=events,
        run_id="run-clean-1",
        worktree_root=worktree,
        network_policy=_policy(),
        workdir=fresh,
        lineage_root=fresh / ".sdd" / "lineage",
        hmac_key=_KEY,
        timestamp=_TS,
    )
    assert clean_run_attestation_path(fresh, attestation.attestation_hash).is_file()


def test_a_symlink_planted_at_the_attestation_leaf_is_not_followed(tmp_path: Path) -> None:
    """A leaf symlink yields no parsed content: refused, not followed.

    A symlink pointing outside the store is refused by containment before
    any open; the no-follow leaf open itself rejects a symlink swapped in
    after path validation (the TOCTOU window) with ELOOP -- pinned against
    the real filesystem, no mocking.
    """
    import errno as _errno

    from bernstein.eval.clean_run import _read_leaf_text, read_clean_run_attestation

    events = _seed_journal(tmp_path, _clean_rows())
    attestation = _build(tmp_path, events)
    leaf = clean_run_attestation_path(tmp_path, attestation.attestation_hash)
    outside = tmp_path / "outside"
    outside.mkdir()
    stolen = outside / "planted.json"
    stolen.write_text(leaf.read_text(encoding="utf-8"), encoding="utf-8")
    leaf.unlink()
    leaf.symlink_to(stolen)

    assert read_clean_run_attestation(tmp_path, attestation.attestation_hash) is None
    result = verify_clean_run_attestation(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        attestation_hash=attestation.attestation_hash,
        journal_events=events,
    )
    assert not result.ok

    # The no-follow open primitive rejects the symlink itself (the race
    # window a resolve-then-open sequence would leave).
    with pytest.raises(OSError) as excinfo:
        _read_leaf_text(leaf)
    assert excinfo.value.errno == _errno.ELOOP


def test_a_second_seal_of_the_same_attestation_is_refused_write_once(tmp_path: Path) -> None:
    """Content-addressed names make the store write-once; a duplicate seal refuses."""
    from bernstein.eval.clean_run import CleanRunError

    events = _seed_journal(tmp_path, _clean_rows())
    _build(tmp_path, events)
    with pytest.raises(CleanRunError, match="write-once"):
        _build(tmp_path, events)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_sealed_attestation_is_operator_only_readable(tmp_path: Path) -> None:
    """The sealed leaf is 0o600 like the lineage log, not group/world readable."""
    import stat

    events = _seed_journal(tmp_path, _clean_rows())
    attestation = _build(tmp_path, events)
    leaf = clean_run_attestation_path(tmp_path, attestation.attestation_hash)
    mode = stat.S_IMODE(leaf.stat().st_mode)
    assert mode & 0o077 == 0, f"attestation must be operator-only, got {mode:#o}"


def test_a_symlink_planted_at_the_leaf_refuses_the_write(tmp_path: Path) -> None:
    """A pre-planted leaf (symlink or file) is never overwritten or followed."""
    from bernstein.eval.clean_run import CleanRunError

    worktree = tmp_path / "wt"
    worktree.mkdir()

    def _build_shared(workdir: Path, events: list[dict[str, object]]):
        return build_clean_run_attestation(
            task=_task(),
            reference_blobs=_reference_blobs(),
            journal_events=events,
            run_id="run-clean-1",
            worktree_root=worktree,
            network_policy=_policy(),
            workdir=workdir,
            lineage_root=workdir / ".sdd" / "lineage",
            hmac_key=_KEY,
            timestamp=_TS,
        )

    # Learn the deterministic hash by sealing in a scratch workdir.
    scratch_events = _seed_journal(tmp_path / "scratch", _clean_rows())
    scratch = _build_shared(tmp_path / "scratch", scratch_events)

    victim = tmp_path / "victim"
    store = victim / ".sdd" / "eval" / "clean_run"
    store.mkdir(parents=True)
    attacker_file = store / "attacker-target.json"
    attacker_file.write_text("{}", encoding="utf-8")
    (store / f"{scratch.attestation_hash}.json").symlink_to(attacker_file)

    victim_events = _seed_journal(victim, _clean_rows())
    with pytest.raises(CleanRunError, match="write-once"):
        _build_shared(victim, victim_events)
    assert attacker_file.read_text(encoding="utf-8") == "{}"


# ---------------------------------------------------------------------------
# Scope boundary + activity extraction unit surface
# ---------------------------------------------------------------------------


def test_scope_boundary_requires_an_existing_directory(tmp_path: Path) -> None:
    with pytest.raises(CleanRunBoundaryError):
        scope_boundary(tmp_path / "missing", _policy())


def test_extract_activity_skips_rows_without_scannable_payload(tmp_path: Path) -> None:
    rows: list[dict[str, object]] = [
        {"event": "task_claimed", "task_id": "t-1"},
        {"event": "file_read", "path": "src/mathlib.py"},
    ]
    events = _seed_journal(tmp_path, rows)
    boundary = scope_boundary(_worktree(tmp_path), _policy())
    activities = extract_activity(events, boundary=boundary, key=_KEY)
    assert [a.index for a in activities] == [1]


def test_scan_activity_is_pure_over_sealed_data(tmp_path: Path) -> None:
    rows = _clean_rows()
    rows.append({"event": "tool_call", "arguments": {"query": _HIDDEN_TEST_TOKEN}})
    events = _seed_journal(tmp_path, rows)
    boundary = scope_boundary(_worktree(tmp_path), _policy())
    contraband = build_contraband_set(_task(), key=_KEY, reference_blobs=_reference_blobs())
    activities = extract_activity(events, boundary=boundary, key=_KEY)
    matches = scan_activity(activities, contraband)
    assert scan_activity(activities, contraband) == matches
    assert any(m.match_class == "contraband_token" for m in matches)
