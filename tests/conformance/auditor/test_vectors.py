"""The 21 auditor questions, as vectors over the recorded bundle.

Six vectors are implemented here: question 17 and the integrity group
(15, 16, 18, 19, 20). Data/endpoint vectors live in test_data_endpoint_vectors.py. The rest are named in
:mod:`tests.conformance.auditor.questions` and land in their own slices;
until then the scoreboard reports them as unanswered, which is the honest
reading of the evidence rather than a weak assertion that passes.

Every vector answers its question from the exported bundle alone. The
bundle reader refuses any path outside the export, so a vector cannot
reach the ``.sdd/`` that produced it.

Five of the integrity questions cannot be answered by today's evidence,
and each is an ``xfail(strict=True)`` naming the field that is missing and
the issue that would add it. Strict matters twice over: the vector never
flatters the score, and the day the field lands the build fails until the
vector is un-marked. No evidence field is added here to make one pass.

Where a question is only partly answerable, the question-marked vector
asserts the whole question and carries the ``xfail``; a separate unmarked
test asserts the part that does hold today, so a regression in the working
half turns something red instead of disappearing into the ``xfail``.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
from typing import TYPE_CHECKING, Any

import cbor2
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from tests.conformance.auditor import offline, recorder

if TYPE_CHECKING:
    from pathlib import Path

    from tests.conformance.auditor.bundle import BundleReader


@pytest.mark.question(17)
def test_q17_bundle_verifies_with_no_network_and_no_bernstein_install(
    bundle_reader: BundleReader,
    trust_anchor: Path,
    auditor_env: offline.AuditorEnvironment,
) -> None:
    """Q17: can the bundle be verified with no network and no install?

    The receipt is verified by ``verify_cli/`` in a subprocess whose
    import path carries the standalone verifier and its two dependencies
    and nothing else - ``bernstein`` is not importable there, which
    :func:`test_verifier_subprocess_cannot_import_bernstein` proves - and
    whose audit hook denies every socket call. The operator's public key
    is pinned from outside the bundle, so a bundle that re-signed itself
    cannot answer this question with its own key.
    """
    receipt = bundle_reader.resolve(recorder.AUDIT_RECEIPT_NAME)
    result = offline.verify_receipt(auditor_env, receipt=receipt, trust_anchor=trust_anchor)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "OVERALL: PASS" in result.stdout
    for check in ("cose", "intoto", "transparency", "subject_binding"):
        assert f"[PASS] {check}" in result.stdout


# ---------------------------------------------------------------------------
# Forging a bundle
#
# Questions 18 and 20 are only worth asking if the attack is real, so the
# helpers below re-issue a receipt the way someone holding the exported
# bundle and nothing else would: recompute the head over whatever events
# they want the record to contain, rebuild every binding that head appears
# in, and sign the lot with a key they generated. Nothing here imports the
# verifier or ``bernstein`` - a forger has neither - so the wire constants
# are restated, which is also what makes a silent format change show up as
# a failing forgery rather than as a passing vector.
# ---------------------------------------------------------------------------

#: Key identifier the forged receipts carry, so a failure names the attacker.
ATTACKER_KID = "attacker-generated-key"

#: Wire constants, restated from the receipt format rather than imported.
_DSSE_PAYLOAD_TYPE = "application/vnd.in-toto+json"
_COSE_SIGN1_TAG = 18
_COSE_ALG_EDDSA = -8
_COSE_CONTENT_TYPE = "application/vnd.bernstein.audit-receipt+json"

#: Audit events that record a choice the run could have made differently.
DECISION_EVENT_TYPES = (
    "agent.delegated",
    "tool.called",
    "data.read",
    "model.request",
    "repo.changed",
)

#: Field names any of which would let a decision be recomputed from its
#: recorded inputs. ``inputs_hash`` is the one the codebase already writes,
#: for access decisions only (``core/security/governance.py``).
INPUTS_DIGEST_FIELDS = ("inputs_hash", "inputs_sha256", "input_digest")

#: Field names any of which would let a model step be replayed and compared.
RECORDED_CONTENT_FIELDS = ("response_sha256", "response_digest", "content_hash", "recording_id")

#: Field names any of which would state what the evidence does not cover.
COVERAGE_STATEMENT_FIELDS = ("coverage", "not_covered", "excluded", "limitations", "gaps")


def _b64(raw: bytes) -> str:
    """Return standard base64 text for *raw*."""
    return base64.b64encode(raw).decode("ascii")


def _b64url(raw: bytes) -> str:
    """Return unpadded base64url text for *raw*, as a JWK member."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _canonical(obj: object) -> bytes:
    """Return canonical JSON bytes: sorted keys, compact separators."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _events_head(events: list[dict[str, Any]]) -> str:
    """Return the range head: SHA-256 over canonical event JSONL."""
    if not events:
        return hashlib.sha256(b"").hexdigest()
    body = "\n".join(json.dumps(event, sort_keys=True, separators=(",", ":")) for event in events)
    return hashlib.sha256((body + "\n").encode("utf-8")).hexdigest()


def _leaf(data: bytes) -> str:
    """Return the RFC 6962 leaf hash of *data*."""
    return hashlib.sha256(b"\x00" + data).hexdigest()


def _node(left: str, right: str) -> str:
    """Return the RFC 6962 internal node over *left* and *right*."""
    return hashlib.sha256(b"\x01" + left.encode() + right.encode()).hexdigest()


def _merkle_root(leaves: list[str]) -> str:
    """Return the Merkle root over *leaves*, promoting a lone odd node."""
    level = list(leaves)
    while len(level) > 1:
        level = [_node(level[i], level[i + 1]) if i + 1 < len(level) else level[i] for i in range(0, len(level), 2)]
    return level[0]


def _audit_path(leaves: list[str], index: int) -> list[dict[str, object]]:
    """Return the inclusion proof for the leaf at *index*."""
    path: list[dict[str, object]] = []
    level = list(leaves)
    position = index
    while len(level) > 1:
        if position % 2 == 1:
            path.append({"hash": level[position - 1], "left": True})
        elif position + 1 < len(level):
            path.append({"hash": level[position + 1], "left": False})
        level = [_node(level[i], level[i + 1]) if i + 1 < len(level) else level[i] for i in range(0, len(level), 2)]
        position //= 2
    return path


#: Journal fields excluded from the payload projection the chain hashes:
#: the wall-clock envelope and the chain fields derived from it.
_JOURNAL_ENVELOPE_FIELDS = frozenset({"ts", "elapsed_s", "index", "prev_hash", "payload_hash", "event_hash"})


def _journal_head(events: list[dict[str, Any]]) -> str:
    """Return the head the journal chain over *events* arrives at.

    Recomputed the way an auditor would, from the two rules the chain
    states: a payload hash over the decision-relevant projection of each
    step, and an event hash linking that payload to its predecessor.

    Args:
        events: Journal rows as the run receipt carries them.

    Returns:
        The final ``event_hash``, or the empty string for no events.
    """
    head = ""
    for index, event in enumerate(events):
        projected = {key: value for key, value in event.items() if key not in _JOURNAL_ENVELOPE_FIELDS}
        payload_hash = hashlib.sha256(
            json.dumps(projected, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        ).hexdigest()
        head = hashlib.sha256(
            _canonical(
                {
                    "prev_hash": head,
                    "event_type": event["event"],
                    "payload_hash": payload_hash,
                    "index": index,
                }
            )
        ).hexdigest()
    return head


def _pae(payload_type: str, payload: bytes) -> bytes:
    """Return the DSSE pre-authentication encoding of *payload*."""
    type_bytes = payload_type.encode("utf-8")
    return b"DSSEv1 %d %s %d %s" % (len(type_bytes), type_bytes, len(payload), payload)


def reissue_receipt(receipt: dict[str, Any], key: Ed25519PrivateKey) -> dict[str, Any]:
    """Return *receipt* re-issued under *key*, bound to its current events.

    Every binding the verifier checks is rebuilt from ``receipt["events"]``:
    the range head, the subject digest, the COSE payload, the in-toto
    statement subject, the Merkle root and the inclusion proof. The result
    is internally consistent and signed end to end - it is a genuine
    receipt for whatever the forger decided the events were, differing from
    the operator's only in which key vouches for it.

    Args:
        receipt: The receipt taken from the bundle, already mutated if the
            forgery is meant to change the record.
        key: The forger's freshly generated signing key.

    Returns:
        The re-issued receipt.
    """
    forged = copy.deepcopy(receipt)
    events: list[dict[str, Any]] = forged["events"]
    head = _events_head(events)

    forged["subject"]["digest"]["sha256"] = head
    forged["range"]["head_sha256"] = head
    public_raw = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    forged["signing"]["key_id"] = ATTACKER_KID
    forged["signing"]["public_key_jwk"] = {
        "alg": "EdDSA",
        "crv": "Ed25519",
        "kid": ATTACKER_KID,
        "kty": "OKP",
        "x": _b64url(public_raw),
    }

    protected = cbor2.dumps(
        {1: _COSE_ALG_EDDSA, 3: _COSE_CONTENT_TYPE, 4: ATTACKER_KID},
        canonical=True,
    )
    payload = bytes.fromhex(head)
    cose_sig = key.sign(cbor2.dumps(["Signature1", protected, b"", payload], canonical=True))
    forged["formats"]["cose"] = {
        "alg": "EdDSA",
        "content_type": _COSE_CONTENT_TYPE,
        "cose_sign1_b64": _b64(cbor2.dumps(cbor2.CBORTag(_COSE_SIGN1_TAG, [protected, {}, payload, cose_sig]))),
        "key_id": ATTACKER_KID,
    }

    statement = json.loads(base64.b64decode(forged["formats"]["intoto"]["payload"]))
    for subject in statement.get("subject", []):
        subject.setdefault("digest", {})["sha256"] = head
    statement_bytes = _canonical(statement)
    forged["formats"]["intoto"] = {
        "payload": _b64(statement_bytes),
        "payloadType": _DSSE_PAYLOAD_TYPE,
        "signatures": [
            {
                "keyid": ATTACKER_KID,
                "sig": _b64(key.sign(_pae(_DSSE_PAYLOAD_TYPE, statement_bytes))),
            }
        ],
    }

    leaves = [_leaf(_canonical(event)) for event in events]
    signed_head = {
        "tree_size": len(leaves),
        "root_hash": _merkle_root(leaves),
        "subject_sha256": head,
    }
    transparency = forged["formats"]["transparency"]
    transparency["signed_tree_head"] = {
        **signed_head,
        "signature_b64": _b64(key.sign(_canonical(signed_head))),
    }
    transparency["inclusion_proof"] = {
        "audit_path": _audit_path(leaves, len(leaves) - 1),
        "leaf_hash": leaves[-1],
        "leaf_index": len(leaves) - 1,
    }
    return forged


def _write_receipt(destination: Path, receipt: dict[str, Any]) -> Path:
    """Write *receipt* to *destination* and return the path."""
    destination.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return destination


@pytest.mark.question(15)
@pytest.mark.xfail(
    strict=True,
    reason=(
        "no receipt states its own coverage gap: neither run-receipt.json nor "
        "audit-receipt.json carries a coverage field naming what the evidence "
        "leaves out, so silence reads as completeness (#4968)"
    ),
)
def test_q15_the_evidence_says_what_it_does_not_cover(bundle_reader: BundleReader) -> None:
    """Q15: does the evidence say what it does **not** cover?

    A reader who is handed the bundle has no way to tell an activity the
    run never performed from an activity the recording never captured.
    Both receipts state what they contain; neither states what they omit.

    The Article 12 pack ships a ``deferred`` list, but that names clauses
    of the regulation the product has not mapped yet - it says nothing
    about this run's evidence, which is what the question asks about.
    """
    for name in (recorder.RUN_RECEIPT_NAME, recorder.AUDIT_RECEIPT_NAME):
        receipt = bundle_reader.read_json(name)
        stated = [field for field in COVERAGE_STATEMENT_FIELDS if receipt.get(field)]
        assert stated, f"{name} never says what it leaves out; looked for {list(COVERAGE_STATEMENT_FIELDS)}"


@pytest.mark.question(16)
@pytest.mark.xfail(
    strict=True,
    reason=(
        "recorded decisions carry no inputs digest: the chain records that a "
        "delegation, tool call, read, model call and repository change "
        "happened, not what they were computed from, so none can be "
        "recomputed and a widened input is invisible (#4213)"
    ),
)
def test_q16_each_decision_can_be_recomputed_from_its_recorded_inputs(
    bundle_reader: BundleReader,
) -> None:
    """Q16: can each decision be recomputed from its recorded inputs?

    ``core/security/governance.py`` already writes an ``inputs_hash`` over
    ``(role, action, bindings)`` for access decisions, and a verifier can
    recompute it, so a binding widened after the fact changes the hash.
    Nothing else does: the decisions this run actually took reach the
    bundle as bare statements of outcome.
    """
    receipt = bundle_reader.read_json(recorder.AUDIT_RECEIPT_NAME)
    decisions = [event for event in receipt["events"] if event.get("event_type") in DECISION_EVENT_TYPES]
    assert decisions, f"the recording holds no decision to recompute; expected {list(DECISION_EVENT_TYPES)}"

    unrecomputable = [
        f"{event['event_type']}({event.get('resource_id', '')})"
        for event in decisions
        if not any(field in event or field in (event.get("details") or {}) for field in INPUTS_DIGEST_FIELDS)
    ]
    assert not unrecomputable, f"no recorded inputs for {unrecomputable}; looked for {list(INPUTS_DIGEST_FIELDS)}"


@pytest.mark.question(18)
@pytest.mark.xfail(
    strict=True,
    reason=(
        "the bundle carries its own verifying key in signing.public_key_jwk, "
        "so a receipt re-issued under a key the forger generated verifies "
        "exactly as well as the operator's; the standalone verifier can pin "
        "a key but nothing in the bundle says which key to pin (#5033)"
    ),
)
def test_q18_a_bundle_resigned_with_a_fresh_key_is_rejected(
    bundle_reader: BundleReader,
    auditor_env: offline.AuditorEnvironment,
    tmp_path: Path,
) -> None:
    """Q18: can a genuine bundle be told from one re-signed by an attacker?

    The forgery is real, not nominal: a keypair is generated inside this
    test and :func:`reissue_receipt` rebuilds every binding the verifier
    checks - COSE payload, DSSE statement, Merkle root, inclusion proof
    and signed tree head - so the result is a fully consistent receipt
    that nobody but the forger vouches for.

    An auditor holding the bundle and nothing else must be able to reject
    it. Today the verifier reads the key out of the receipt it is checking
    and reports ``trust-on-first-use``, which is not a trust decision.
    """
    genuine = bundle_reader.read_json(recorder.AUDIT_RECEIPT_NAME)
    forged = reissue_receipt(genuine, Ed25519PrivateKey.generate())
    receipt_path = _write_receipt(tmp_path / "resigned-audit-receipt.json", forged)

    result = offline.verify_receipt(auditor_env, receipt=receipt_path)

    assert result.returncode != 0, (
        "a bundle re-signed with a freshly generated key verified from the "
        f"bundle alone:\n{result.stdout}{result.stderr}"
    )


@pytest.mark.question(19)
@pytest.mark.xfail(
    strict=True,
    reason=(
        "the signed journal records that a model response arrived, not what "
        "it contained: no response digest and no recording identifier reach "
        "the bundle, because the replay recorder is opt-in "
        "(BERNSTEIN_RECORD, core/replay/gateway.py) and off by default, so "
        "there is nothing to re-execute against (#5107)"
    ),
)
def test_q19_the_run_can_be_replayed_from_what_the_bundle_records(
    bundle_reader: BundleReader,
) -> None:
    """Q19: can the run be replayed from the evidence?

    Replay needs the responses the run consumed. The journal binds each
    step into a hash chain, so a changed input does show up - that half is
    held by :func:`test_a_changed_recorded_input_diverges_from_the_signed_head`
    - but nothing in the bundle carries the provider output a second
    execution would have to be fed, so there is no second execution to
    compare against.
    """
    receipt = bundle_reader.read_json(recorder.RUN_RECEIPT_NAME)
    responses = [event for event in receipt["journal"]["events"] if event.get("event") == "model_response"]
    assert responses, "the recording holds no model response to replay"

    unreplayable = [
        event["index"] for event in responses if not any(field in event for field in RECORDED_CONTENT_FIELDS)
    ]
    assert not unreplayable, (
        f"model responses at {unreplayable} record no content to replay against; "
        f"looked for {list(RECORDED_CONTENT_FIELDS)}"
    )


@pytest.mark.question(20)
@pytest.mark.xfail(
    strict=True,
    reason=(
        "an edited record can be re-issued end to end under a generated key "
        "and the bundle-only verifier accepts it; the per-event HMAC that "
        "would catch the edit can only be checked with the operator's "
        "symmetric audit key, which is the same key that writes the chain "
        "(core/security/audit.py load_audit_key), so nothing an auditor may "
        "safely hold distinguishes the two (#5036)"
    ),
)
def test_q20_an_edited_record_cannot_be_passed_off_as_the_original(
    bundle_reader: BundleReader,
    auditor_env: offline.AuditorEnvironment,
    tmp_path: Path,
) -> None:
    """Q20: can the evidence show the record was not edited after the fact?

    Say plainly what this question turns on. The bundle offers two
    integrity witnesses and an auditor can rely on neither:

    * the per-event ``hmac`` chain is symmetric - verifying it requires the
      operator's audit key, and that key also writes valid events, so an
      auditor who holds enough to check the chain holds enough to forge it;
    * the Ed25519 signatures are asymmetric, but the verifying key travels
      inside the receipt, so re-issuing under another key costs nothing.

    The edit here is the one that matters: the read of the sensitive file
    is rewritten to name an innocuous one. A naive edit is caught, because
    the subject digest no longer matches the events - that much holds
    today. The same edit re-issued under a generated key is not.
    """
    genuine = bundle_reader.read_json(recorder.AUDIT_RECEIPT_NAME)
    edited = copy.deepcopy(genuine)
    reads = [event for event in edited["events"] if event.get("event_type") == "data.read"]
    assert reads, "the recording holds no sensitive read to edit out"
    reads[0]["resource_id"] = "config/public_defaults.yaml"
    reads[0]["details"] = {"sensitivity": "public"}

    naive = offline.verify_receipt(
        auditor_env,
        receipt=_write_receipt(tmp_path / "edited-audit-receipt.json", edited),
    )
    assert naive.returncode != 0, f"an edited event passed unchanged bindings:\n{naive.stdout}"

    reissued = offline.verify_receipt(
        auditor_env,
        receipt=_write_receipt(
            tmp_path / "reissued-audit-receipt.json",
            reissue_receipt(edited, Ed25519PrivateKey.generate()),
        ),
    )
    assert reissued.returncode != 0, (
        "an edited record, re-issued under a generated key, verified from the "
        f"bundle alone:\n{reissued.stdout}{reissued.stderr}"
    )


def test_a_pinned_trust_anchor_rejects_a_bundle_resigned_with_another_key(
    bundle_reader: BundleReader,
    trust_anchor: Path,
    auditor_env: offline.AuditorEnvironment,
    tmp_path: Path,
) -> None:
    """The half of question 18 that does hold: a pin, given out of band, works.

    The same forged receipt is verified twice. Pinned against the
    operator's public key - which the auditor received separately, not
    from the bundle - it is rejected. Unpinned it verifies and the
    verifier says so: ``trust-on-first-use``.

    The unpinned pass is also the check on the forgery itself. A sloppy
    re-issue would fail for the wrong reason and make
    :func:`test_q18_a_bundle_resigned_with_a_fresh_key_is_rejected` look
    like a finding when it was only a broken fixture.
    """
    forged = reissue_receipt(bundle_reader.read_json(recorder.AUDIT_RECEIPT_NAME), Ed25519PrivateKey.generate())
    receipt_path = _write_receipt(tmp_path / "resigned-audit-receipt.json", forged)

    pinned = offline.verify_receipt(auditor_env, receipt=receipt_path, trust_anchor=trust_anchor)
    assert pinned.returncode != 0, f"the pinned key accepted a foreign signer:\n{pinned.stdout}"
    assert "does not match the pinned" in pinned.stdout

    unpinned = offline.verify_receipt(auditor_env, receipt=receipt_path)
    assert unpinned.returncode == 0, (
        f"the forgery is malformed, not merely unauthorised:\n{unpinned.stdout}{unpinned.stderr}"
    )
    assert "OVERALL: PASS" in unpinned.stdout
    assert "trust-on-first-use" in unpinned.stdout


def test_a_changed_recorded_input_diverges_from_the_signed_head(bundle_reader: BundleReader) -> None:
    """The half of question 19 that does hold: divergence is detectable.

    The journal is a hash chain over the decision-relevant projection of
    each step, so a second execution that consumed a different input
    chains to a different head. Recomputing the chain from the bundle
    reproduces the signed head exactly; flipping one recorded input - the
    endpoint the delegated agent called - moves the head, and every event
    after it.
    """
    journal = bundle_reader.read_json(recorder.RUN_RECEIPT_NAME)["journal"]
    events = journal["events"]

    assert _journal_head(events) == journal["head_hash"], "the recomputed chain does not reproduce the signed head"

    diverged = copy.deepcopy(events)
    changed = next(event for event in diverged if event.get("event") == "model_request")
    changed["endpoint"] = "https://elsewhere.example.invalid/v1"

    assert _journal_head(diverged) != journal["head_hash"], "a changed recorded input left the head unmoved"


def test_the_only_per_event_witness_in_the_bundle_is_a_symmetric_hmac(
    bundle_reader: BundleReader,
) -> None:
    """What question 20 rests on, asserted rather than asserted about.

    Every event's own integrity witness is an HMAC, and the receipt names
    no anchor outside itself: no issuer, no certificate, no chain. The
    only asymmetric key in the bundle is the one the receipt asserts about
    its own signature.
    """
    receipt = bundle_reader.read_json(recorder.AUDIT_RECEIPT_NAME)

    for event in receipt["events"]:
        assert "hmac" in event and "prev_hmac" in event, f"event without an HMAC witness: {event.get('event_type')}"
        signed = [field for field in ("signature", "signature_b64", "sig", "public_key_jwk") if field in event]
        assert not signed, f"unexpected per-event signature fields {signed}"

    anchors = [field for field in ("issuer", "trust_anchor", "certificate_chain", "x5c") if field in receipt]
    assert not anchors, f"the receipt does name an outside anchor after all: {anchors}"
    assert receipt["signing"]["public_key_jwk"]["kid"] == receipt["signing"]["key_id"]
