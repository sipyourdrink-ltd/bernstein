"""Unit tests for the signed run receipt (issue #2924).

The signed subject is derived from the embedded journal/spine/audit
ranges, every head is recomputed by the verifier from the receipt bytes
alone (no HMAC key, no ``.sdd/``), and any single mutation - a journal
row, a spine entry, an audit event, a signature byte, or a stripped
range - collapses verification.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein.core.lineage.spine import LineageSpine
from bernstein.core.replay.journal import EventJournal
from bernstein.core.replay.run_receipt import (
    _GENESIS,  # pyright: ignore[reportPrivateUsage]
    RUN_RECEIPT_FILENAME,
    RunReceiptError,
    _walk_spine_rows,  # pyright: ignore[reportPrivateUsage]
    build_run_receipt,
    verify_run_receipt,
    write_run_receipt_if_configured,
)
from bernstein.core.security.lineage_kms import FileBasedKMSAdapter

if TYPE_CHECKING:
    from bernstein.core.security.lineage_kms import KMSAdapter

_RUN_ID = "run-receipt-fixture"
_HMAC_KEY = b"x" * 32
_SIGN_SEED = b"i" * 32
_OTHER_SEED = b"o" * 32


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _seed_run(sdd_dir: Path, run_id: str = _RUN_ID) -> None:
    """Populate a hermetic run: 3 journal events + 2 spine entries."""
    journal = EventJournal(run_id=run_id, sdd_dir=sdd_dir)
    journal.record("run_started", run_id=run_id)
    journal.record("task_claimed", task_id="T-1", role="backend")
    journal.record("run_completed", run_id=run_id, ticks=7)
    spine = LineageSpine(sdd_dir / "lineage", run_id=run_id, hmac_key=_HMAC_KEY)
    spine.record(
        artifact_path="src/app.py",
        content=b"print('hi')\n",
        actor="backend",
        step_id="T-1",
        model="m1",
        timestamp=1111,
    )
    spine.record(
        artifact_path="tests/test_app.py",
        content=b"assert True\n",
        actor="qa",
        step_id="T-2",
        model="m1",
        timestamp=2222,
    )


def _write_key(path: Path, seed: bytes) -> Ed25519PrivateKey:
    key = Ed25519PrivateKey.from_private_bytes(seed)
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )
    return key


def _kms(tmp_path: Path, seed: bytes = _SIGN_SEED) -> KMSAdapter:
    key_path = tmp_path / f"sign-{seed[:1].hex()}.pem"
    _write_key(key_path, seed)
    return FileBasedKMSAdapter(key_path, kid="test-run-receipt-key")


def _public_pem(seed: bytes) -> bytes:
    key = Ed25519PrivateKey.from_private_bytes(seed)
    return key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _reserialize(doc: dict[str, Any]) -> bytes:
    """Re-encode a (possibly mutated) receipt dict; format is irrelevant to verify."""
    return json.dumps(doc).encode("utf-8")


# ---------------------------------------------------------------------------
# Round trip + determinism
# ---------------------------------------------------------------------------


def test_receipt_round_trip_verifies_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The receipt verifies from its bytes alone: no .sdd/, no HMAC key.

    Verification runs from an unrelated empty cwd so any hidden dependence
    on the live run directory would surface as a failure.
    """
    sdd = tmp_path / "proj" / ".sdd"
    _seed_run(sdd)
    receipt = build_run_receipt(_RUN_ID, sdd, _kms(tmp_path))
    assert receipt.receipt_path is not None
    assert receipt.receipt_path.name == RUN_RECEIPT_FILENAME
    receipt_bytes = receipt.receipt_path.read_bytes()

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    result = verify_run_receipt(receipt_bytes)
    assert result.ok
    assert result.status == "ok"
    assert result.run_id == _RUN_ID
    assert result.journal_events == 3
    assert result.spine_entries == 2


def test_receipt_bytes_are_byte_identical_across_independent_builds(tmp_path: Path) -> None:
    """Two independent builds over the same run produce SHA-256-equal bytes."""
    sdd = tmp_path / ".sdd"
    _seed_run(sdd)
    first = build_run_receipt(_RUN_ID, sdd, _kms(tmp_path))
    second = build_run_receipt(_RUN_ID, sdd, _kms(tmp_path), write=False)
    assert hashlib.sha256(first.receipt_bytes).hexdigest() == hashlib.sha256(second.receipt_bytes).hexdigest()


def test_receipt_excludes_wall_clock_fields(tmp_path: Path) -> None:
    """No journal wall-clock byte enters the receipt: ts/elapsed_s are stripped."""
    sdd = tmp_path / ".sdd"
    _seed_run(sdd)
    receipt = build_run_receipt(_RUN_ID, sdd, _kms(tmp_path), write=False)
    for row in receipt.receipt["journal"]["events"]:
        assert "ts" not in row
        assert "elapsed_s" not in row


# ---------------------------------------------------------------------------
# Tamper detection and offline verification
# ---------------------------------------------------------------------------


def test_tampered_journal_row_fails_at_exact_divergent_step(tmp_path: Path) -> None:
    """Mutating one embedded journal row reports tamper at its 0-based index."""
    sdd = tmp_path / ".sdd"
    _seed_run(sdd)
    receipt = build_run_receipt(_RUN_ID, sdd, _kms(tmp_path), write=False)

    doc = json.loads(receipt.receipt_bytes)
    doc["journal"]["events"][1]["task_id"] = "T-FORGED"
    result = verify_run_receipt(_reserialize(doc))
    assert not result.ok
    assert result.status == "tampered"
    assert result.divergent_step == 1


def test_stripped_event_range_fails_closed(tmp_path: Path) -> None:
    """Removing the embedded journal range makes the subject unreachable.

    The receipt is the proof, not a log beside it: with the range gone
    verification must fail (never pass with a warning), whether the key is
    deleted outright or emptied while the head is still asserted.
    """
    sdd = tmp_path / ".sdd"
    _seed_run(sdd)
    receipt = build_run_receipt(_RUN_ID, sdd, _kms(tmp_path), write=False)

    stripped = json.loads(receipt.receipt_bytes)
    del stripped["journal"]["events"]
    result = verify_run_receipt(_reserialize(stripped))
    assert not result.ok
    assert result.status == "malformed"

    emptied = json.loads(receipt.receipt_bytes)
    emptied["journal"]["events"] = []
    result_empty = verify_run_receipt(_reserialize(emptied))
    assert not result_empty.ok
    assert result_empty.status in {"malformed", "tampered"}


def test_flipped_signature_byte_fails_signature_check(tmp_path: Path) -> None:
    """A one-byte edit to the detached Ed25519 signature fails verification."""
    sdd = tmp_path / ".sdd"
    _seed_run(sdd)
    receipt = build_run_receipt(_RUN_ID, sdd, _kms(tmp_path), write=False)

    doc = json.loads(receipt.receipt_bytes)
    sig = bytearray(base64.b64decode(doc["signing"]["signature_b64"]))
    sig[0] ^= 0x01
    doc["signing"]["signature_b64"] = base64.b64encode(bytes(sig)).decode("ascii")
    result = verify_run_receipt(_reserialize(doc))
    assert not result.ok
    assert result.status == "tampered"
    assert any("signature" in err for err in result.errors)


def test_wrong_public_key_rejected(tmp_path: Path) -> None:
    """A pin for a different Ed25519 key fails even though content recomputes."""
    sdd = tmp_path / ".sdd"
    _seed_run(sdd)
    receipt = build_run_receipt(_RUN_ID, sdd, _kms(tmp_path), write=False)

    # Sanity: the right pin passes.
    good = verify_run_receipt(receipt.receipt_bytes, public_key_pem=_public_pem(_SIGN_SEED))
    assert good.ok

    result = verify_run_receipt(receipt.receipt_bytes, public_key_pem=_public_pem(_OTHER_SEED))
    assert not result.ok
    assert result.status == "tampered"
    assert any("pinned" in err for err in result.errors)


def test_spine_chain_recomputes_without_hmac_key(tmp_path: Path) -> None:
    """Spine entries embed no keyed hmac field, yet the chain fully recomputes.

    The verifier re-derives every entry_hash and the head from entry bodies
    alone; a mutated spine entry collapses verification.
    """
    sdd = tmp_path / ".sdd"
    _seed_run(sdd)
    receipt = build_run_receipt(_RUN_ID, sdd, _kms(tmp_path), write=False)

    entries = receipt.receipt["spine"]["entries"]
    assert len(entries) == 2
    assert all("hmac" not in entry for entry in entries)
    assert verify_run_receipt(receipt.receipt_bytes).ok

    doc = json.loads(receipt.receipt_bytes)
    doc["spine"]["entries"][0]["artifact_path"] = "src/evil.py"
    result = verify_run_receipt(_reserialize(doc))
    assert not result.ok
    assert result.status == "tampered"
    assert any("spine entry 0" in err for err in result.errors)


def test_asserted_head_cannot_outvote_recomputed_head(tmp_path: Path) -> None:
    """Editing the asserted journal head (leaving rows intact) is tamper.

    Guards the binding-not-decoration property: the verifier trusts only
    what it recomputes, so an asserted head that disagrees with the
    embedded rows can never pass.
    """
    sdd = tmp_path / ".sdd"
    _seed_run(sdd)
    receipt = build_run_receipt(_RUN_ID, sdd, _kms(tmp_path), write=False)

    doc = json.loads(receipt.receipt_bytes)
    doc["journal"]["head_hash"] = "0" * 64
    result = verify_run_receipt(_reserialize(doc))
    assert not result.ok
    assert result.status == "tampered"


# ---------------------------------------------------------------------------
# Optional audit range
# ---------------------------------------------------------------------------


def _seed_audit(sdd_dir: Path) -> None:
    from bernstein.core.security.audit import AuditLog

    audit_dir = sdd_dir / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    log = AuditLog(audit_dir, key=_HMAC_KEY)
    log.log("task.created", "alice", "task", "T-1", {"role": "backend"})
    log.log("task.completed", "alice", "task", "T-1", {"status": "ok"})


def test_audit_range_round_trip_and_strip_collapse(tmp_path: Path) -> None:
    """The opt-in audit range verifies offline; stripping the block is tamper.

    The binding block omits (rather than nulls) the audit head when the
    block is absent, so a receipt signed WITH the range no longer matches
    its subject once the range is removed.
    """
    sdd = tmp_path / ".sdd"
    _seed_run(sdd)
    _seed_audit(sdd)
    receipt = build_run_receipt(
        _RUN_ID,
        sdd,
        _kms(tmp_path),
        include_audit_range=True,
        audit_hmac_key=_HMAC_KEY,
        audit_since="2020-01-01T00:00:00.000000Z",
        audit_until="2100-01-01T00:00:00.000000Z",
        write=False,
    )
    assert receipt.audit_head_sha256 is not None
    assert receipt.receipt["audit_range"]["event_count"] == 2
    assert verify_run_receipt(receipt.receipt_bytes).ok

    stripped = json.loads(receipt.receipt_bytes)
    del stripped["audit_range"]
    result = verify_run_receipt(_reserialize(stripped))
    assert not result.ok
    assert result.status == "tampered"

    mutated = json.loads(receipt.receipt_bytes)
    mutated["audit_range"]["events"][0]["action"] = "task.forged"
    result_mut = verify_run_receipt(_reserialize(mutated))
    assert not result_mut.ok
    assert result_mut.status == "tampered"


def test_audit_range_requires_build_inputs(tmp_path: Path) -> None:
    """include_audit_range without key/window is refused, never half-built."""
    sdd = tmp_path / ".sdd"
    _seed_run(sdd)
    with pytest.raises(RunReceiptError, match="include_audit_range"):
        build_run_receipt(_RUN_ID, sdd, _kms(tmp_path), include_audit_range=True, write=False)


# ---------------------------------------------------------------------------
# Build refusals (never sign nothing, never sign a broken chain)
# ---------------------------------------------------------------------------


def test_empty_run_refuses_to_build(tmp_path: Path) -> None:
    """A run with no journal events has no identity to attest."""
    sdd = tmp_path / ".sdd"
    with pytest.raises(RunReceiptError, match="no journal events"):
        build_run_receipt("run-empty", sdd, _kms(tmp_path), write=False)


def test_build_refuses_to_sign_tampered_journal(tmp_path: Path) -> None:
    """A journal whose on-disk chain fails recompute is never signed."""
    sdd = tmp_path / ".sdd"
    _seed_run(sdd)
    journal_path = sdd / "runs" / _RUN_ID / "journal.jsonl"
    lines = journal_path.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[1])
    row["task_id"] = "T-FORGED"
    lines[1] = json.dumps(row)
    journal_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(RunReceiptError, match="journal chain fails at step 1"):
        build_run_receipt(_RUN_ID, sdd, _kms(tmp_path), write=False)


@pytest.mark.parametrize("corrupt_line_index", [1, 2], ids=["middle-row", "trailing-row"])
def test_malformed_journal_row_refuses_receipt_build(tmp_path: Path, corrupt_line_index: int) -> None:
    """An unparseable journal line refuses the build, naming the physical line.

    The trailing-row case is the load-bearing one: the tolerant shared
    loader would silently drop it and the surviving prefix chains cleanly
    from genesis, so without the strict signing-path parse the receipt
    would attest a shorter run with a wrong event_count and head. The
    build must refuse instead, and no receipt file may exist afterwards.
    """
    sdd = tmp_path / ".sdd"
    _seed_run(sdd)
    journal_path = sdd / "runs" / _RUN_ID / "journal.jsonl"
    lines = journal_path.read_text(encoding="utf-8").splitlines()
    lines[corrupt_line_index] = '{"truncated": '
    journal_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(RunReceiptError, match=f"journal line {corrupt_line_index + 1} is not valid JSON"):
        build_run_receipt(_RUN_ID, sdd, _kms(tmp_path))
    assert not (sdd / "runs" / _RUN_ID / RUN_RECEIPT_FILENAME).exists()


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda row: row.update(index=str(row["index"])), "non-integer 'index'"),
        (lambda row: row.update(event=None), "non-string 'event'"),
        (lambda row: row.pop("payload_hash"), "empty 'payload_hash'"),
        (lambda row: row.update(event_hash=""), "empty 'event_hash'"),
    ],
    ids=["stringified-index", "null-event", "missing-payload-hash", "empty-event-hash"],
)
def test_mistyped_journal_chain_field_refuses_receipt_build(tmp_path: Path, mutate, match: str) -> None:
    """A row with a missing or mistyped chain field is refused before signing.

    Downstream hashing normalises with str() and projections drop absent
    fields, so without this validation a null event or a stripped
    payload_hash would be embedded verbatim into the signed receipt as a
    non-canonical journal event instead of refusing the build.
    """
    sdd = tmp_path / ".sdd"
    _seed_run(sdd)
    journal_path = sdd / "runs" / _RUN_ID / "journal.jsonl"
    lines = journal_path.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[1])
    mutate(row)
    lines[1] = json.dumps(row)
    journal_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(RunReceiptError, match=f"journal line 2 has a missing or {match}"):
        build_run_receipt(_RUN_ID, sdd, _kms(tmp_path))
    assert not (sdd / "runs" / _RUN_ID / RUN_RECEIPT_FILENAME).exists()


@pytest.mark.parametrize(
    ("bad_line", "match"),
    [
        ('{"garbage": ', "spine line 2 is not valid JSON"),
        ('{"v": 1, "prev_hash": "x"}', "spine line 2 is missing fields"),
    ],
    ids=["malformed-json", "bad-shape"],
)
def test_incomplete_spine_never_signed(tmp_path: Path, bad_line: str, match: str) -> None:
    """A malformed or bad-shape spine row refuses the build - never skipped.

    ``iter_entries`` tolerantly drops such rows; on a trailing row the
    remaining prefix still chains, so signing through it would attest an
    incomplete spine. The strict signing-path reader refuses instead, and
    no receipt file may exist afterwards.
    """
    sdd = tmp_path / ".sdd"
    _seed_run(sdd)
    spine_path = sdd / "lineage" / _RUN_ID / "spine.jsonl"
    lines = spine_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    lines[1] = bad_line
    spine_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(RunReceiptError, match=match):
        build_run_receipt(_RUN_ID, sdd, _kms(tmp_path))
    assert not (sdd / "runs" / _RUN_ID / RUN_RECEIPT_FILENAME).exists()


def test_walk_spine_rows_rejects_unknown_scheme_version() -> None:
    """An entry with an unsupported ``v`` is rejected, not treated as v1.

    A spine row carrying an unknown scheme version must not silently hash as
    v1 (which would let a forged row verify under the bare preimage); the
    walker reports it as a divergence instead.
    """
    rows = [
        {
            "v": 99,
            "prev_hash": _GENESIS,
            "artifact_path": "src/app.py",
            "content_hash": "c",
            "actor": "backend",
            "step_id": "T-1",
            "model": "m1",
            "timestamp": 1111,
            "entry_hash": "h",
        }
    ]
    head, idx, error = _walk_spine_rows(rows)
    assert idx == 0
    assert "unsupported scheme version" in error
    assert head == _GENESIS


# ---------------------------------------------------------------------------
# Finalization hook degradation
# ---------------------------------------------------------------------------


def test_seal_hook_helper_is_documented_noop_without_signing_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No signing key configured -> no receipt, no crash (never over-promise)."""
    monkeypatch.delenv("BERNSTEIN_RUN_RECEIPT_SIGNING_KEY_PATH", raising=False)
    monkeypatch.delenv("BERNSTEIN_RUN_RECEIPT_SIGNING_ENV_VAR", raising=False)
    sdd = tmp_path / ".sdd"
    _seed_run(sdd)

    assert write_run_receipt_if_configured(_RUN_ID, sdd) is None
    assert not (sdd / "runs" / _RUN_ID / RUN_RECEIPT_FILENAME).exists()


def test_seal_hook_helper_writes_receipt_when_key_configured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the env-configured key the hook writes a verifying receipt."""
    key_path = tmp_path / "sign.pem"
    _write_key(key_path, _SIGN_SEED)
    monkeypatch.setenv("BERNSTEIN_RUN_RECEIPT_SIGNING_KEY_PATH", str(key_path))
    monkeypatch.delenv("BERNSTEIN_RUN_RECEIPT_SIGNING_ENV_VAR", raising=False)
    sdd = tmp_path / ".sdd"
    _seed_run(sdd)

    receipt_path = write_run_receipt_if_configured(_RUN_ID, sdd)
    assert receipt_path is not None
    assert receipt_path == sdd / "runs" / _RUN_ID / RUN_RECEIPT_FILENAME
    assert verify_run_receipt(receipt_path.read_bytes()).ok


class _SealHookStub:
    """Just enough of ``Orchestrator`` for its unbound seal-hook method.

    Records which downstream hooks ran so the tests can assert the receipt
    write is gated on seal success.
    """

    def __init__(self, workdir: Path, journal: EventJournal) -> None:
        self._workdir = workdir
        self._recorder = journal
        self._run_id = journal.run_id
        self.calls: list[str] = []

    def _record_run_branch_provenance(self, hmac_key: bytes) -> None:
        self.calls.append("provenance")

    def _seal_intent_capsules(self, hmac_key: bytes) -> None:
        self.calls.append("capsules")

    def _write_run_receipt(self) -> None:
        self.calls.append("receipt")


def test_failed_spine_seal_produces_no_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When sealing the journal head into the spine fails, no receipt is written.

    Fail-closed: a receipt must never be signed over a spine whose journal
    binding is incomplete. The seal failure is simulated at the audit-key
    load, the first step of the hook's try block.
    """
    from bernstein.core.orchestration.orchestrator import Orchestrator

    sdd = tmp_path / ".sdd"
    journal = EventJournal(run_id="run-hook-fail", sdd_dir=sdd)
    journal.record("run_started")
    stub = _SealHookStub(tmp_path, journal)

    def _boom() -> bytes:
        raise OSError("audit key store unavailable")

    monkeypatch.setattr("bernstein.core.security.audit.load_or_create_audit_key", _boom)

    Orchestrator._seal_journal_into_lineage_spine(stub)  # type: ignore[arg-type]
    assert "receipt" not in stub.calls


def test_successful_seal_writes_receipt_via_hook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The success branch of the seal hook reaches the receipt write."""
    from bernstein.core.orchestration.orchestrator import Orchestrator

    sdd = tmp_path / ".sdd"
    journal = EventJournal(run_id="run-hook-ok", sdd_dir=sdd)
    journal.record("run_started")
    stub = _SealHookStub(tmp_path, journal)

    monkeypatch.setattr("bernstein.core.security.audit.load_or_create_audit_key", lambda: b"k" * 32)

    Orchestrator._seal_journal_into_lineage_spine(stub)  # type: ignore[arg-type]
    assert "receipt" in stub.calls


def test_branch_provenance_is_recorded_before_the_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Artifact rows must be in the spine before the receipt binds its head.

    ``build_run_receipt`` reads the spine head as it stands when it runs, so
    rows recorded after the receipt would sit outside what it attests - the
    run would ship a receipt that covers a spine missing the run's own work.
    """
    from bernstein.core.orchestration.orchestrator import Orchestrator

    sdd = tmp_path / ".sdd"
    journal = EventJournal(run_id="run-hook-order", sdd_dir=sdd)
    journal.record("run_started")
    stub = _SealHookStub(tmp_path, journal)

    monkeypatch.setattr("bernstein.core.security.audit.load_or_create_audit_key", lambda: b"k" * 32)

    Orchestrator._seal_journal_into_lineage_spine(stub)  # type: ignore[arg-type]
    assert stub.calls.index("provenance") < stub.calls.index("receipt")


def test_branch_provenance_absorbs_a_failing_lineage_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provenance failure must not travel far enough to cost the receipt.

    The rows are an aid and stay re-derivable from the branch; the receipt is
    the run's attested identity. The recording sits outside the seal's ``try``
    so its failure is never reported as a seal failure - which leaves this
    method's own handler as the thing that keeps it from reaching the caller
    and skipping the receipt write that follows it.
    """
    from bernstein.core.orchestration.orchestrator import Orchestrator

    journal = EventJournal(run_id="run-prov-fail", sdd_dir=tmp_path / ".sdd")
    journal.record("run_started")
    stub = _SealHookStub(tmp_path, journal)

    def _boom(**_kwargs: object) -> None:
        raise OSError("lineage store unavailable")

    monkeypatch.setattr(
        "bernstein.core.lineage.merge_provenance.record_run_branch_artifacts",
        _boom,
    )

    Orchestrator._record_run_branch_provenance(stub, b"k" * 32)  # type: ignore[arg-type]
