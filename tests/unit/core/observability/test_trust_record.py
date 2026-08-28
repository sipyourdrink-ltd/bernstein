"""Tests for :mod:`bernstein.core.observability.trust_record`.

Focused tests for the TRACE 0.2 Trust Record emitter functionality.
Tests cover journal parsing, claim construction, signing, and canonical output.

Issue #4692 (spec-review corrections against agentrust-io/trace-spec#231)
added the ``subject`` (did:key) rework, ``enforce``, ``runtime``,
``references[]``, ``appraisal``, and the ``parent_record_hash`` delegation
chain. Tests for those six corrections are named for the property they
protect, grouped in dedicated classes below the pre-existing coverage.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from bernstein.core.observability.trust_record import (
    _MULTICODEC_ED25519_PUB,
    TrustRecord,
    TrustRecordEmitter,
    _base58btc_decode,
    _base58btc_encode,
    _did_key_from_ed25519_public_key,
    _sign_canonical_bytes_detached,
)
from bernstein.core.replay.journal import (
    _GENESIS_HASH,
    _payload_hash,
    compute_event_hash,
)
from bernstein.core.security.agent_card_signer import (
    _b64url,
    canonicalize_jcs,
    verify_detached_jws_over_canonical,
)

#: Fixed 32-byte Ed25519 seed for tests that need to independently recompute
#: the expected public key material (as opposed to the per-test isolated but
#: *unknown* key the autouse ``_isolate_agent_card_keystore`` fixture wires
#: up). Never used outside the test tree.
_TEST_SEED = b"t" * 32

_TRUST_RECORD_TYP = "trust-record+jws"

#: Every top-level field an emitted record's signature must cover, in the
#: order ``_sign_record`` builds the signing body. Kept as one tuple so the
#: field-surface tests below and the offline verifier agree on the shape.
_SIGNED_BODY_FIELDS: tuple[str, ...] = (
    "subject",
    "enforce",
    "runtime",
    "references",
    "appraisal",
    "parent_record_hash",
    "claims",
)


def _create_journal(tmp_path: Path, events: list[dict]) -> Path:
    """Create a journal.jsonl file with the given events, chained properly.

    Each entry in *events* is a decision payload (``{"type": ..., ...}``).
    The helper builds the Merkle chain fields (``prev_hash``,
    ``payload_hash``, ``event_hash``, ``index``) from the payload so that
    :func:`verify_events` accepts the file. A bare ``event_hash`` on the
    payload is dropped: the chain fields own the head hash.
    """
    journal = tmp_path / "journal.jsonl"
    lines: list[str] = []
    prev_hash = _GENESIS_HASH
    for index, payload in enumerate(events):
        event_type = str(payload.get("type", "event"))
        chain_payload = {k: v for k, v in payload.items() if k != "event_hash"}
        p_hash = _payload_hash(event_type, chain_payload)
        e_hash = compute_event_hash(
            prev_hash=prev_hash,
            event_type=event_type,
            payload_hash=p_hash,
            index=index,
        )
        entry = {
            "index": index,
            "event": event_type,
            "prev_hash": prev_hash,
            "payload_hash": p_hash,
            "event_hash": e_hash,
        }
        entry.update(chain_payload)
        lines.append(json.dumps(entry, sort_keys=True))
        prev_hash = e_hash
    journal.write_text("\n".join(lines) + "\n" if lines else "")
    return journal


def _test_keypair() -> tuple[bytes, bytes]:
    """Return ``(private_key_pem, public_key_raw_32_bytes)`` for a fixed seed.

    Deterministic (unlike the per-test autouse keystore) so a test can
    independently recompute the expected did:key subject and cross-check it
    against what the emitter produced.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.from_private_bytes(_TEST_SEED)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_raw = key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return private_pem, public_raw


def _public_key_pem_from_raw(public_raw: bytes) -> bytes:
    """Re-wrap a raw 32-byte Ed25519 public key as SPKI PEM."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    return Ed25519PublicKey.from_public_bytes(public_raw).public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _emitter_with_known_key(*, install_rev: str = "aaaaaaaaaaaaaaaa") -> TrustRecordEmitter:
    """Return an emitter whose signing key and install rev are fixed and known."""
    private_pem, _ = _test_keypair()
    return TrustRecordEmitter(
        install_rev_getter=lambda: install_rev,
        get_private_key_pem=lambda: private_pem,
    )


def _canonical_body_bytes(doc: dict[str, Any]) -> bytes:
    """Rebuild the exact bytes ``_sign_record`` signed from a parsed record."""
    body = {field: doc[field] for field in _SIGNED_BODY_FIELDS}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _rebuild_detached_jws(signature: dict[str, str]) -> str:
    """Rebuild the compact detached JWS string from a record's ``signature`` object."""
    header = {"alg": signature["alg"], "typ": _TRUST_RECORD_TYP, "kid": signature["kid"]}
    header_b64 = _b64url(canonicalize_jcs(header))
    return f"{header_b64}..{signature['sig']}"


def _offline_verify(doc: dict[str, Any], public_key_pem: bytes) -> bool:
    """Re-verify a parsed record's signature over its full field surface, offline.

    Reuses the production ``verify_detached_jws_over_canonical`` helper so
    this test does not carry a second, hand-rolled verification path that
    could disagree with the signer.
    """
    return verify_detached_jws_over_canonical(
        _canonical_body_bytes(doc),
        _rebuild_detached_jws(doc["signature"]),
        public_key_pem,
        expected_typ=_TRUST_RECORD_TYP,
    )


def _public_key_from_did_key(subject: str) -> bytes:
    """Recover the raw 32-byte Ed25519 public key from a ``did:key`` subject.

    Strips any DID URL path/fragment suffix (e.g. ``/run/<id>``), the
    ``did:key:`` prefix and the multibase ``z`` marker, base58btc-decodes,
    and drops the 2-byte ``ed25519-pub`` multicodec prefix. Mirrors what an
    independent verifier does: recover the signing key from the subject
    itself, with no registry or side-channel key file.
    """
    method_specific_id = subject.removeprefix("did:key:").split("/", 1)[0]
    assert method_specific_id.startswith("z"), f"not a base58btc multibase value: {method_specific_id!r}"
    decoded = _base58btc_decode(method_specific_id[1:])
    assert decoded[:2] == _MULTICODEC_ED25519_PUB
    return decoded[2:]


# ---------------------------------------------------------------------------
# TrustRecordEmitter._build_unsigned_record
# ---------------------------------------------------------------------------


class TestBuildUnsignedRecord:
    def test_empty_journal_returns_zero_counts(self, tmp_path: Path) -> None:
        emitter = TrustRecordEmitter()
        journal = _create_journal(tmp_path, [])
        record = emitter._build_unsigned_record(journal, "run-123")

        assert record.subject.startswith("did:key:z")
        assert record.subject.endswith("/run/run-123")
        assert record.claims == {
            "run_id": "run-123",
            "event_count": 0,
            "head_hash": "",
        }
        assert record.signature == {}

    def test_single_event_populates_head_hash_and_timestamps(self, tmp_path: Path) -> None:
        emitter = TrustRecordEmitter()
        # The journal format must match the internal chain: each row needs type
        # and any optional payload (but NOT event_hash, which is computed).
        events = [{"type": "start", "ts": 1000.0}]
        journal = _create_journal(tmp_path, events)
        record = emitter._build_unsigned_record(journal, "run-456")

        assert record.claims["event_count"] == 1
        # head_hash is computed from the chain; we trust it's present and non-empty.
        assert record.claims["head_hash"] != ""
        assert record.claims["first_event_ts"] == 1000.0
        assert record.claims["last_event_ts"] == 1000.0

    def test_multiple_events_records_first_and_last_timestamps(self, tmp_path: Path) -> None:
        emitter = TrustRecordEmitter()
        events = [
            {"type": "first", "ts": 1000.0},
            {"type": "middle", "ts": 2000.0},
            {"type": "last", "ts": 3000.0},
        ]
        journal = _create_journal(tmp_path, events)
        record = emitter._build_unsigned_record(journal, "run-789")

        assert record.claims["event_count"] == 3
        assert record.claims["head_hash"] != ""
        assert record.claims["first_event_ts"] == 1000.0
        assert record.claims["last_event_ts"] == 3000.0

    def test_events_without_timestamps_omits_ts_fields(self, tmp_path: Path) -> None:
        emitter = TrustRecordEmitter()
        events = [{"type": "info"}]
        journal = _create_journal(tmp_path, events)
        record = emitter._build_unsigned_record(journal, "run-no-ts")

        assert "first_event_ts" not in record.claims
        assert "last_event_ts" not in record.claims

    def test_malformed_json_lines_skipped(self, tmp_path: Path) -> None:
        emitter = TrustRecordEmitter()
        # Build two valid chained events, then interleave a malformed line.
        events = [{"type": "valid"}, {"type": "also_valid"}]
        journal = _create_journal(tmp_path, events)
        # Insert a malformed line between events 0 and 1 (appended, not in
        # the chain): the tolerant reader must skip it and keep the chain
        # intact for the two real events.
        raw_lines = journal.read_text(encoding="utf-8").strip().splitlines()
        raw_lines.insert(1, "not json")
        journal.write_text("\n".join(raw_lines) + "\n")
        record = emitter._build_unsigned_record(journal, "run-malformed")

        assert record.claims["event_count"] == 2

    def test_missing_journal_returns_empty_record(self, tmp_path: Path) -> None:
        emitter = TrustRecordEmitter()
        missing = tmp_path / "nonexistent.jsonl"
        record = emitter._build_unsigned_record(missing, "run-miss")

        assert record.claims["event_count"] == 0
        assert record.claims["head_hash"] == ""

    def test_a_journal_with_a_broken_chain_is_refused(self, tmp_path: Path) -> None:
        """A tampered journal (mutated prev_hash) must not produce a record.

        The error must name the divergent step index (R12), not merely
        report a bare true/false.
        """
        emitter = TrustRecordEmitter()
        # Build a valid two-event journal, then corrupt the second event's
        # prev_hash so the chain breaks at step 1.
        events = [{"type": "event_1"}, {"type": "event_2"}]
        journal = _create_journal(tmp_path, events)
        raw = json.loads(journal.read_text(encoding="utf-8").splitlines()[1])
        raw["prev_hash"] = "deadbeef" * 8
        lines = journal.read_text(encoding="utf-8").strip().splitlines()
        lines[1] = json.dumps(raw, sort_keys=True)
        journal.write_text("\n".join(lines) + "\n")

        with pytest.raises(ValueError, match="journal chain broken"):
            emitter._build_unsigned_record(journal, "run-broken")


# ---------------------------------------------------------------------------
# subject: did:key URI (issue #4692, corrections table row 1)
# ---------------------------------------------------------------------------


class TestSubjectDidKeyUri:
    """``subject`` must be a self-certifying did:key URI, run-scoped.

    did:key was chosen over the codebase's existing SPIFFE machinery
    (``bernstein.core.identity.spiffe``) because every SPIFFE derivation in
    this repository requires an operator-supplied trust domain with no
    default anywhere (CLI ``--trust-domain`` is ``required=True``, and the
    live path needs a reachable SPIRE Workload API) -- exactly the
    "invented trust domain" the did:key choice avoids. did:key needs only
    the install's own Ed25519 key, which the emitter already carries.
    """

    def test_subject_is_a_spiffe_or_did_uri(self, tmp_path: Path) -> None:
        emitter = _emitter_with_known_key()
        journal = _create_journal(tmp_path, [{"type": "run_start", "ts": 1.0}])
        output = emitter.emit_trust_record(journal, "run-a")
        subject = json.loads(output)["subject"]

        assert subject.startswith("did:key:z")

    def test_subject_is_no_longer_a_bare_run_urn(self, tmp_path: Path) -> None:
        """Pins the corrections-table regression: the old scheme must not reappear."""
        emitter = _emitter_with_known_key()
        journal = _create_journal(tmp_path, [])
        output = emitter.emit_trust_record(journal, "my-run-id")
        subject = json.loads(output)["subject"]

        assert not subject.startswith("urn:bernstein:run:")

    def test_subject_multibase_key_matches_the_install_public_key(self, tmp_path: Path) -> None:
        private_pem, public_raw = _test_keypair()
        emitter = TrustRecordEmitter(
            install_rev_getter=lambda: "aaaaaaaaaaaaaaaa",
            get_private_key_pem=lambda: private_pem,
        )
        journal = _create_journal(tmp_path, [])
        output = emitter.emit_trust_record(journal, "run-key-match")
        subject = json.loads(output)["subject"]

        assert _public_key_from_did_key(subject) == public_raw

    def test_subject_is_scoped_to_the_run_id(self, tmp_path: Path) -> None:
        emitter = _emitter_with_known_key()
        journal = _create_journal(tmp_path, [])
        output_a = emitter.emit_trust_record(journal, "run-alpha")
        output_b = emitter.emit_trust_record(journal, "run-beta")
        subject_a = json.loads(output_a)["subject"]
        subject_b = json.loads(output_b)["subject"]

        assert subject_a.endswith("/run/run-alpha")
        assert subject_b.endswith("/run/run-beta")
        # Same install key -> identical did:key prefix; only the run suffix differs.
        assert subject_a.split("/run/")[0] == subject_b.split("/run/")[0]

    def test_did_key_prefix_is_stable_for_any_ed25519_key(self) -> None:
        """Every Ed25519 did:key begins ``did:key:z6Mk``: the 2-byte multicodec
        prefix dominates the leading base58 digits regardless of the trailing
        32 key bytes. A sanity check independent of the round-trip property
        tests, and of the emitter itself (calls the derivation directly)."""
        _private_pem, public_raw = _test_keypair()

        assert _did_key_from_ed25519_public_key(public_raw).startswith("did:key:z6Mk")

    def test_two_installs_with_different_keys_get_different_subjects(self, tmp_path: Path) -> None:
        private_pem_a, _ = _test_keypair()
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        key_b = Ed25519PrivateKey.from_private_bytes(b"z" * 32)
        private_pem_b = key_b.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        emitter_a = TrustRecordEmitter(get_private_key_pem=lambda: private_pem_a)
        emitter_b = TrustRecordEmitter(get_private_key_pem=lambda: private_pem_b)
        journal = _create_journal(tmp_path, [])

        subject_a = json.loads(emitter_a.emit_trust_record(journal, "same-run"))["subject"]
        subject_b = json.loads(emitter_b.emit_trust_record(journal, "same-run"))["subject"]

        assert subject_a != subject_b


# ---------------------------------------------------------------------------
# base58btc (multibase) encoding -- the primitive `subject` derivation uses
# ---------------------------------------------------------------------------


class TestBase58btc:
    def test_alphabet_excludes_visually_ambiguous_characters(self) -> None:
        """Bitcoin/multibase base58 drops 0, O, I, l to avoid transcription errors."""
        from bernstein.core.observability.trust_record import _BASE58BTC_ALPHABET

        assert len(_BASE58BTC_ALPHABET) == 58
        assert len(set(_BASE58BTC_ALPHABET)) == 58
        for ambiguous in "0OIl":
            assert ambiguous not in _BASE58BTC_ALPHABET

    @pytest.mark.parametrize(
        "data",
        [
            b"",
            b"\x00",
            b"\x00\x00\x01",
            b"hello world",
            _MULTICODEC_ED25519_PUB + bytes(range(32)),
            bytes([0xFF]) * 32,
        ],
        ids=["empty", "single-zero", "leading-zeros", "ascii", "multicodec-ed25519", "all-0xff"],
    )
    def test_round_trips_arbitrary_bytes(self, data: bytes) -> None:
        assert _base58btc_decode(_base58btc_encode(data)) == data

    def test_encoding_is_deterministic(self) -> None:
        data = b"\x01\x02\x03deterministic"
        assert _base58btc_encode(data) == _base58btc_encode(data)


# ---------------------------------------------------------------------------
# enforce (issue #4692, corrections table row 2)
# ---------------------------------------------------------------------------


class TestEnforceClaim:
    def test_enforce_claim_is_present(self, tmp_path: Path) -> None:
        emitter = _emitter_with_known_key()
        journal = _create_journal(tmp_path, [])
        output = emitter.emit_trust_record(journal, "run-enforce")
        parsed = json.loads(output)

        assert "enforce" in parsed

    def test_enforce_is_not_spelled_enforced(self, tmp_path: Path) -> None:
        """Pins the corrections-table regression: the old misspelling must not reappear."""
        emitter = _emitter_with_known_key()
        journal = _create_journal(tmp_path, [])
        parsed = json.loads(emitter.emit_trust_record(journal, "run-spelling"))

        assert "enforced" not in parsed
        assert "enforce" in parsed


# ---------------------------------------------------------------------------
# runtime: platform + measurement (issue #4692, corrections table row 3)
# ---------------------------------------------------------------------------


class TestRuntimeClaim:
    def test_runtime_claim_is_present_with_platform_and_measurement(self, tmp_path: Path) -> None:
        emitter = _emitter_with_known_key()
        journal = _create_journal(tmp_path, [{"type": "run_start", "ts": 1.0}])
        parsed = json.loads(emitter.emit_trust_record(journal, "run-runtime"))

        assert "runtime" in parsed
        assert isinstance(parsed["runtime"]["platform"], str)
        assert parsed["runtime"]["platform"] != ""
        assert isinstance(parsed["runtime"]["measurement"], str)

    def test_runtime_measurement_is_the_sealed_journal_head_hash(self, tmp_path: Path) -> None:
        """Design constraint: runtime.measurement carries the sealed journal head hash."""
        emitter = _emitter_with_known_key()
        events = [{"type": "run_start", "ts": 1.0}, {"type": "run_end", "ts": 2.0}]
        journal = _create_journal(tmp_path, events)
        parsed = json.loads(emitter.emit_trust_record(journal, "run-measurement"))

        assert parsed["runtime"]["measurement"] == parsed["claims"]["head_hash"]
        assert parsed["runtime"]["measurement"] != ""

    def test_runtime_platform_identifies_software_not_hardware(self, tmp_path: Path) -> None:
        """Design constraint: this producer is software-only, never hardware-attested."""
        emitter = _emitter_with_known_key()
        journal = _create_journal(tmp_path, [])
        parsed = json.loads(emitter.emit_trust_record(journal, "run-software"))

        platform_claim = parsed["runtime"]["platform"].lower()
        for hardware_term in ("tpm", "sgx", "sev", "tee", "hsm"):
            assert hardware_term not in platform_claim


# ---------------------------------------------------------------------------
# references[]: rel/id/resolver (issue #4692, corrections table row 4)
# ---------------------------------------------------------------------------


class TestReferencesClaim:
    def test_references_entries_carry_rel_id_and_resolver(self, tmp_path: Path) -> None:
        emitter = _emitter_with_known_key()
        journal = _create_journal(tmp_path, [{"type": "run_start", "ts": 1.0}])
        parsed = json.loads(emitter.emit_trust_record(journal, "run-refs"))

        assert "references" in parsed
        assert len(parsed["references"]) >= 1
        for entry in parsed["references"]:
            assert isinstance(entry["rel"], str) and entry["rel"]
            assert isinstance(entry["id"], str) and entry["id"]
            assert isinstance(entry["resolver"], str) and entry["resolver"]

    def test_references_includes_a_self_evidence_entry_for_the_journal(self, tmp_path: Path) -> None:
        emitter = _emitter_with_known_key()
        journal = _create_journal(tmp_path, [{"type": "run_start", "ts": 1.0}])
        parsed = json.loads(emitter.emit_trust_record(journal, "run-evidence"))

        evidence = [r for r in parsed["references"] if r["rel"] == "evidence"]
        assert len(evidence) == 1
        assert evidence[0]["id"] == f"sha256:{parsed['claims']['head_hash']}"

    def test_references_is_empty_for_a_journal_with_no_events(self, tmp_path: Path) -> None:
        """No head hash to point at -> no hollow evidence entry (matches how
        first_event_ts/last_event_ts are omitted rather than emitted empty)."""
        emitter = _emitter_with_known_key()
        journal = _create_journal(tmp_path, [])
        parsed = json.loads(emitter.emit_trust_record(journal, "run-no-events"))

        assert parsed["references"] == []


# ---------------------------------------------------------------------------
# appraisal: status + verifier (issue #4692, corrections table row 5)
# ---------------------------------------------------------------------------


class TestAppraisalClaim:
    def test_appraisal_carries_status_and_verifier_uri(self, tmp_path: Path) -> None:
        emitter = _emitter_with_known_key()
        journal = _create_journal(tmp_path, [{"type": "run_start", "ts": 1.0}])
        parsed = json.loads(emitter.emit_trust_record(journal, "run-appraisal"))

        assert isinstance(parsed["appraisal"]["status"], str) and parsed["appraisal"]["status"]
        assert isinstance(parsed["appraisal"]["verifier"], str)
        assert parsed["appraisal"]["verifier"].startswith("did:key:")

    def test_appraisal_status_is_affirming_when_the_journal_chain_verified(self, tmp_path: Path) -> None:
        """The only appraisal this producer can honestly perform is its own
        journal-chain check -- which already gates record construction
        (see test_a_journal_with_a_broken_chain_is_refused)."""
        emitter = _emitter_with_known_key()
        journal = _create_journal(tmp_path, [{"type": "run_start", "ts": 1.0}])
        parsed = json.loads(emitter.emit_trust_record(journal, "run-affirm"))

        assert parsed["appraisal"]["status"] == "affirming"

    def test_appraisal_verifier_is_the_producer_itself_not_a_third_party(self, tmp_path: Path) -> None:
        """Self-attestation, not independent third-party appraisal -- see the
        module docstring's seal-boundary sentence."""
        emitter = _emitter_with_known_key()
        journal = _create_journal(tmp_path, [{"type": "run_start", "ts": 1.0}])
        parsed = json.loads(emitter.emit_trust_record(journal, "run-self-appraise"))

        assert parsed["appraisal"]["verifier"] == parsed["subject"]


# ---------------------------------------------------------------------------
# delegation via parent_record_hash (issue #4692, corrections table row 6)
# ---------------------------------------------------------------------------


class TestDelegationChain:
    def _emit_parent_and_child(self, tmp_path: Path) -> tuple[TrustRecordEmitter, str, str]:
        emitter = _emitter_with_known_key()
        parent_dir = tmp_path / "parent"
        parent_dir.mkdir()
        child_dir = tmp_path / "child"
        child_dir.mkdir()
        parent_journal = _create_journal(parent_dir, [{"type": "run_start", "ts": 1.0}])
        child_journal = _create_journal(child_dir, [{"type": "task_spawn", "ts": 2.0}])

        parent_output = emitter.emit_trust_record(parent_journal, "parent-run")
        child_output = emitter.emit_trust_record(child_journal, "child-run", parent_record=parent_output)
        return emitter, parent_output, child_output

    def test_root_record_has_no_parent_record_hash(self, tmp_path: Path) -> None:
        emitter = _emitter_with_known_key()
        journal = _create_journal(tmp_path, [])
        parsed = json.loads(emitter.emit_trust_record(journal, "root-run"))

        assert parsed["parent_record_hash"] is None

    def test_delegated_child_parent_record_hash_equals_sha256_of_parent_canonical_record(self, tmp_path: Path) -> None:
        _emitter, parent_output, child_output = self._emit_parent_and_child(tmp_path)

        expected = hashlib.sha256(parent_output.encode("utf-8")).hexdigest()
        assert json.loads(child_output)["parent_record_hash"] == expected

    def test_delegated_execution_emits_one_record_per_hop(self, tmp_path: Path) -> None:
        _emitter, parent_output, child_output = self._emit_parent_and_child(tmp_path)
        parent_doc = json.loads(parent_output)
        child_doc = json.loads(child_output)

        assert parent_doc["claims"]["run_id"] == "parent-run"
        assert child_doc["claims"]["run_id"] == "child-run"
        assert parent_output != child_output
        assert parent_doc["subject"] != child_doc["subject"]

    def test_delegated_child_references_a_predecessor_pointing_at_the_parent_subject(self, tmp_path: Path) -> None:
        _emitter, parent_output, child_output = self._emit_parent_and_child(tmp_path)
        parent_doc = json.loads(parent_output)
        child_doc = json.loads(child_output)

        predecessors = [r for r in child_doc["references"] if r["rel"] == "predecessor"]
        assert len(predecessors) == 1
        assert predecessors[0]["id"] == f"sha256:{child_doc['parent_record_hash']}"
        assert predecessors[0]["resolver"] == parent_doc["subject"]

    def test_malformed_parent_record_is_refused(self, tmp_path: Path) -> None:
        emitter = _emitter_with_known_key()
        journal = _create_journal(tmp_path, [])

        with pytest.raises(ValueError, match="parent_record"):
            emitter.emit_trust_record(journal, "run-bad-parent", parent_record="not json")


# ---------------------------------------------------------------------------
# Signature coverage: the new fields must be inside the signed body, not
# decorative additions alongside it.
# ---------------------------------------------------------------------------


def _mutate_enforce(doc: dict[str, Any]) -> None:
    doc["enforce"] = not doc["enforce"]


def _mutate_runtime(doc: dict[str, Any]) -> None:
    doc["runtime"] = dict(doc["runtime"])
    doc["runtime"]["platform"] = "tampered-platform"


def _mutate_references(doc: dict[str, Any]) -> None:
    doc["references"] = [*doc["references"], {"rel": "tampered", "id": "x", "resolver": "y"}]


def _mutate_appraisal(doc: dict[str, Any]) -> None:
    doc["appraisal"] = dict(doc["appraisal"])
    doc["appraisal"]["status"] = "contraindicated"


def _mutate_parent_record_hash(doc: dict[str, Any]) -> None:
    doc["parent_record_hash"] = "0" * 64


def _mutate_subject(doc: dict[str, Any]) -> None:
    doc["subject"] = doc["subject"] + "-tampered"


class TestSignatureCoversFullFieldSurface:
    @pytest.mark.parametrize(
        "mutate",
        [_mutate_enforce, _mutate_runtime, _mutate_references, _mutate_appraisal, _mutate_parent_record_hash],
        ids=["enforce", "runtime", "references", "appraisal", "parent_record_hash"],
    )
    def test_tampering_a_new_field_after_signing_invalidates_the_signature(self, tmp_path: Path, mutate: Any) -> None:
        private_pem, public_raw = _test_keypair()
        emitter = TrustRecordEmitter(
            install_rev_getter=lambda: "aaaaaaaaaaaaaaaa",
            get_private_key_pem=lambda: private_pem,
        )
        journal = _create_journal(tmp_path, [{"type": "run_start", "ts": 1.0}])
        doc = json.loads(emitter.emit_trust_record(journal, "run-tamper"))
        public_key_pem = _public_key_pem_from_raw(public_raw)

        assert _offline_verify(doc, public_key_pem) is True, "sanity: the untampered record must verify"

        mutate(doc)

        assert _offline_verify(doc, public_key_pem) is False

    def test_tampering_subject_after_signing_invalidates_the_signature(self, tmp_path: Path) -> None:
        # Kept separate: subject already had signature coverage before #4692
        # (it was the run URN), this just re-confirms it still holds under
        # the did:key rework.
        private_pem, public_raw = _test_keypair()
        emitter = TrustRecordEmitter(
            install_rev_getter=lambda: "aaaaaaaaaaaaaaaa",
            get_private_key_pem=lambda: private_pem,
        )
        journal = _create_journal(tmp_path, [])
        doc = json.loads(emitter.emit_trust_record(journal, "run-tamper-subject"))
        public_key_pem = _public_key_pem_from_raw(public_raw)

        _mutate_subject(doc)

        assert _offline_verify(doc, public_key_pem) is False


# ---------------------------------------------------------------------------
# TrustRecordEmitter._sign_record
# ---------------------------------------------------------------------------


def _bare_record(**overrides: Any) -> TrustRecord:
    base: dict[str, Any] = {
        "subject": "did:key:zTestOnly/run/test",
        "enforce": True,
        "runtime": {"platform": "cpython-test", "measurement": "deadbeef"},
        "references": [],
        "appraisal": {"status": "affirming", "verifier": "did:key:zTestOnly/run/test"},
        "parent_record_hash": None,
        "claims": {"run_id": "test"},
        "signature": {},
    }
    base.update(overrides)
    return TrustRecord(**base)


class TestSignRecord:
    def test_signature_contains_eddsa_alg(self, tmp_path: Path) -> None:
        emitter = TrustRecordEmitter()
        record = _bare_record()
        signed = emitter._sign_record(record, "test-key-id")

        assert signed.signature["alg"] == "EdDSA"
        assert signed.signature["kid"] == "test-key-id"
        assert signed.signature["sig"] != ""

    def test_signature_is_base64url(self, tmp_path: Path) -> None:
        emitter = TrustRecordEmitter()
        record = _bare_record()
        signed = emitter._sign_record(record, "test-key-id")

        sig = signed.signature["sig"]
        # Base64url alphabet (A-Z, a-z, 0-9, -, _), possibly with padding
        import re

        assert re.match(r"^[A-Za-z0-9_-]*={0,2}$", sig)

    def test_record_payload_unchanged_after_signing(self, tmp_path: Path) -> None:
        emitter = TrustRecordEmitter()
        record = _bare_record(claims={"run_id": "test", "count": 5}, parent_record_hash="ab" * 32)
        signed = emitter._sign_record(record, "kid-1")

        assert signed.subject == record.subject
        assert signed.enforce == record.enforce
        assert signed.runtime == record.runtime
        assert signed.references == record.references
        assert signed.appraisal == record.appraisal
        assert signed.parent_record_hash == record.parent_record_hash
        assert signed.claims == record.claims


# ---------------------------------------------------------------------------
# _sign_canonical_bytes_detached
# ---------------------------------------------------------------------------


class TestSignCanonicalBytesDetached:
    def test_produces_jws_format(self, tmp_path: Path) -> None:
        """Output should be header..signature format (RFC 7515 compact)."""
        # Generate a key for testing
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )

        private_key = Ed25519PrivateKey.generate()
        private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

        canonical_bytes = b'{"a":1,"b":2}'
        typ = "test-type"
        kid = "test-kid"

        jws = _sign_canonical_bytes_detached(canonical_bytes, private_pem, typ, kid)

        # Format: base64url(header)..base64url(signature)
        parts = jws.split(".")
        assert len(parts) == 3
        assert parts[1] == ""  # Empty body slot in detached JWS

    def test_jws_header_contains_typ_and_kid(self, tmp_path: Path) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )

        private_key = Ed25519PrivateKey.generate()
        private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

        canonical_bytes = b"test"
        typ = "application/test"
        kid = "custom-kid"

        jws = _sign_canonical_bytes_detached(canonical_bytes, private_pem, typ, kid)

        import base64

        header_b64 = jws.split(".")[0]
        # Add padding for decoding
        padded = header_b64 + "=" * (4 - len(header_b64) % 4)
        header = json.loads(base64.urlsafe_b64decode(padded))
        assert header["typ"] == typ
        assert header["kid"] == kid
        assert header["alg"] == "EdDSA"

    def test_different_payload_different_signature(self, tmp_path: Path) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )

        private_key = Ed25519PrivateKey.generate()
        private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

        sig1 = _sign_canonical_bytes_detached(b"payload1", private_pem, "t", "k")
        sig2 = _sign_canonical_bytes_detached(b"payload2", private_pem, "t", "k")

        assert sig1 != sig2

    def test_different_key_different_signature(self, tmp_path: Path) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )

        private_key_1 = Ed25519PrivateKey.generate()
        private_pem_1 = private_key_1.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

        private_key_2 = Ed25519PrivateKey.generate()
        private_pem_2 = private_key_2.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

        payload = b"same payload"
        sig1 = _sign_canonical_bytes_detached(payload, private_pem_1, "t", "k")
        sig2 = _sign_canonical_bytes_detached(payload, private_pem_2, "t", "k")

        assert sig1 != sig2


# ---------------------------------------------------------------------------
# TrustRecordEmitter.emit_trust_record
# ---------------------------------------------------------------------------


class TestEmitTrustRecord:
    def test_output_is_canonical_json(self, tmp_path: Path) -> None:
        """Canonical JSON: sorted keys, minimal separators."""
        emitter = TrustRecordEmitter()
        events = [{"ts": 1000.0, "event_hash": "hash1", "type": "start"}]
        journal = _create_journal(tmp_path, events)

        output = emitter.emit_trust_record(journal, "run-emit")

        # Should be valid JSON
        parsed = json.loads(output)

        # Re-serialize with same options and compare
        expected = json.dumps(parsed, sort_keys=True, separators=(",", ":"))
        assert output == expected

    def test_output_contains_all_required_fields(self, tmp_path: Path) -> None:
        emitter = TrustRecordEmitter()
        events = [{"ts": 1000.0, "event_hash": "h1"}]
        journal = _create_journal(tmp_path, events)

        output = emitter.emit_trust_record(journal, "run-fields")
        parsed = json.loads(output)

        assert "subject" in parsed
        assert "enforce" in parsed
        assert "runtime" in parsed
        assert "references" in parsed
        assert "appraisal" in parsed
        assert "parent_record_hash" in parsed
        assert "claims" in parsed
        assert "signature" in parsed

    def test_subject_is_no_longer_a_bare_run_urn(self, tmp_path: Path) -> None:
        emitter = TrustRecordEmitter()
        events = []
        journal = _create_journal(tmp_path, events)

        output = emitter.emit_trust_record(journal, "my-run-id")
        parsed = json.loads(output)

        assert parsed["subject"] != "urn:bernstein:run:my-run-id"
        assert parsed["subject"].startswith("did:key:")
        assert parsed["subject"].endswith("/run/my-run-id")

    def test_delegation_field_is_removed_in_favor_of_parent_record_hash(self, tmp_path: Path) -> None:
        """Pins the corrections-table row: delegation (string) -> parent_record_hash."""
        emitter = TrustRecordEmitter()
        events = []
        journal = _create_journal(tmp_path, events)

        output = emitter.emit_trust_record(journal, "run-delegate")
        parsed = json.loads(output)

        assert "delegation" not in parsed
        assert "parent_record_hash" in parsed
        assert parsed["parent_record_hash"] is None

    def test_signature_alg_is_eddsa(self, tmp_path: Path) -> None:
        emitter = TrustRecordEmitter()
        events = []
        journal = _create_journal(tmp_path, events)

        output = emitter.emit_trust_record(journal, "run-sig")
        parsed = json.loads(output)

        assert parsed["signature"]["alg"] == "EdDSA"

    def test_full_round_trip_produces_valid_signature(self, tmp_path: Path) -> None:
        emitter = TrustRecordEmitter()
        events = [{"ts": 1.0, "event_hash": "final"}]
        journal = _create_journal(tmp_path, events)

        output = emitter.emit_trust_record(journal, "run-verify")
        parsed = json.loads(output)

        # Verify signature structure
        sig = parsed["signature"]
        assert "kid" in sig
        assert "sig" in sig
        assert sig["alg"] == "EdDSA"
        assert sig["sig"] != ""


# ---------------------------------------------------------------------------
# Integration: full emit flow
# ---------------------------------------------------------------------------


class TestFullEmitFlow:
    def test_emit_trust_record_end_to_end(self, tmp_path: Path) -> None:
        """End-to-end test: journal -> trust record -> signed output."""
        private_pem, public_raw = _test_keypair()
        emitter = TrustRecordEmitter(
            install_rev_getter=lambda: "aaaaaaaaaaaaaaaa",
            get_private_key_pem=lambda: private_pem,
        )

        # Create a realistic journal
        events = [
            {"ts": 1690000000.0, "type": "run_start"},
            {"ts": 1690000001.0, "type": "task_spawn"},
            {"ts": 1690000002.0, "type": "task_complete"},
        ]
        journal = _create_journal(tmp_path, events)

        output = emitter.emit_trust_record(journal, "integration-run")

        parsed = json.loads(output)

        # Claims
        assert parsed["claims"]["run_id"] == "integration-run"
        assert parsed["claims"]["event_count"] == 3
        # head_hash is computed from the chain; the important property is
        # that it is non-empty and reproducible from the journal.
        assert parsed["claims"]["head_hash"] != ""
        assert parsed["claims"]["first_event_ts"] == 1690000000.0
        assert parsed["claims"]["last_event_ts"] == 1690000002.0

        # Subject: self-certifying did:key, run-scoped
        assert parsed["subject"].startswith("did:key:z")
        assert parsed["subject"].endswith("/run/integration-run")
        assert _public_key_from_did_key(parsed["subject"]) == public_raw

        # The six #4692 corrections
        assert parsed["enforce"] is True
        assert parsed["runtime"] == {
            "platform": parsed["runtime"]["platform"],
            "measurement": parsed["claims"]["head_hash"],
        }
        assert parsed["references"][0]["rel"] == "evidence"
        assert parsed["appraisal"]["status"] == "affirming"
        assert parsed["appraisal"]["verifier"] == parsed["subject"]
        assert parsed["parent_record_hash"] is None

        # Signature
        assert parsed["signature"]["alg"] == "EdDSA"
        assert parsed["signature"]["kid"].startswith("install-")
        assert len(parsed["signature"]["sig"]) > 0
        assert _offline_verify(parsed, _public_key_pem_from_raw(public_raw)) is True

    def test_empty_journal_produces_valid_record(self, tmp_path: Path) -> None:
        emitter = TrustRecordEmitter()
        journal = _create_journal(tmp_path, [])

        output = emitter.emit_trust_record(journal, "empty-run")
        parsed = json.loads(output)

        assert parsed["claims"]["event_count"] == 0
        assert parsed["claims"]["head_hash"] == ""
        assert parsed["signature"]["sig"] != ""  # Still signed
        assert parsed["parent_record_hash"] is None
        assert parsed["references"] == []


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


class TestErrorCases:
    def test_journal_with_only_whitespace_lines(self, tmp_path: Path) -> None:
        emitter = TrustRecordEmitter()
        journal = tmp_path / "journal.jsonl"
        journal.write_text("   \n\t\n   \n")

        record = emitter._build_unsigned_record(journal, "run-whitespace")
        assert record.claims["event_count"] == 0

    def test_journal_with_empty_lines_only(self, tmp_path: Path) -> None:
        emitter = TrustRecordEmitter()
        journal = tmp_path / "journal.jsonl"
        journal.write_text("\n\n")

        record = emitter._build_unsigned_record(journal, "run-empty")
        assert record.claims["event_count"] == 0


# ---------------------------------------------------------------------------
# Determinism: same journal, byte-identical unsigned payload
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_the_same_journal_yields_a_byte_identical_unsigned_payload(self, tmp_path: Path) -> None:
        """Two emitter calls on the same journal must produce identical bytes.

        Determinism is structurally guaranteed (JSON sorted keys, compact
        separators, ``json.loads`` round-trip preserves float identity), but
        this test pins the invariant with an inter-process comparison so a
        future refactor cannot silently break it.

        Relies on the autouse ``_isolate_agent_card_keystore`` fixture
        (``tests/conftest.py``) pointing every process spawned during this
        test -- including the two subprocesses below -- at the same per-test
        keystore directory, so all three calls sign (and derive the did:key
        subject from) the same install key.
        """
        import subprocess
        import sys

        events = [
            {"ts": 1690000000.0, "type": "run_start"},
            {"ts": 1690000001.0, "type": "task_spawn"},
            {"ts": 1690000002.0, "type": "task_complete"},
        ]
        journal = _create_journal(tmp_path, events)
        run_id = "determinism-run"

        # Spawn two independent subprocesses that each call
        # _build_unsigned_record and serialize with the same canonical
        # options; byte-identical output across processes is the invariant.
        snippet = (
            "import json, sys\n"
            "from pathlib import Path\n"
            "from bernstein.core.observability.trust_record import TrustRecordEmitter\n"
            "journal = Path(sys.argv[1])\n"
            "run_id = sys.argv[2]\n"
            "record = TrustRecordEmitter()._build_unsigned_record(journal, run_id)\n"
            "body = {\n"
            "    'subject': record.subject,\n"
            "    'enforce': record.enforce,\n"
            "    'runtime': record.runtime,\n"
            "    'references': record.references,\n"
            "    'appraisal': record.appraisal,\n"
            "    'parent_record_hash': record.parent_record_hash,\n"
            "    'claims': record.claims,\n"
            "    'signature': record.signature,\n"
            "}\n"
            "print(json.dumps(body, sort_keys=True, separators=(',', ':')))\n"
        )
        # Compare the canonical bytes from two independent subprocesses
        first = subprocess.run(
            [sys.executable, "-c", snippet, str(journal), run_id],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.rstrip("\n")
        second = subprocess.run(
            [sys.executable, "-c", snippet, str(journal), run_id],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.rstrip("\n")
        # Compare the canonical bytes for determinism
        assert first == second

        # And a second call on the same process must match the subprocess
        # output exactly.
        in_process = TrustRecordEmitter()._build_unsigned_record(journal, run_id)
        in_process_body = {
            "subject": in_process.subject,
            "enforce": in_process.enforce,
            "runtime": in_process.runtime,
            "references": in_process.references,
            "appraisal": in_process.appraisal,
            "parent_record_hash": in_process.parent_record_hash,
            "claims": in_process.claims,
            "signature": in_process.signature,
        }
        in_process_bytes = json.dumps(in_process_body, sort_keys=True, separators=(",", ":"))
        assert in_process_bytes == first


# ---------------------------------------------------------------------------
# Core install unchanged without the [trace] extra
# ---------------------------------------------------------------------------


class TestCoreInstallWithoutTraceExtra:
    def test_importing_bernstein_does_not_import_agentrust_trace(self) -> None:
        """Importing bernstein must not pull in agentrust_trace.

        The trace extra is optional; a future refactor that accidentally
        adds a top-level import would silently reintroduce the transitive
        dependency, so this test pins the guard with a subprocess.
        """
        import subprocess
        import sys

        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys, bernstein; print([m for m in sys.modules if 'agentrust' in m])",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        # The list must be empty: no agentrust module may be loaded.
        assert proc.stdout.rstrip("\n") == "[]"


# ---------------------------------------------------------------------------
# Module docstring states the seal boundary (issue #4692 acceptance criterion)
# ---------------------------------------------------------------------------


class TestModuleDocstringStatesTheSealBoundary:
    def test_docstring_states_what_the_seal_can_and_cannot_prove(self) -> None:
        import bernstein.core.observability.trust_record as trust_record_module

        doc = trust_record_module.__doc__ or ""
        assert "cannot prove" in doc
        assert "signed software evidence" in doc
