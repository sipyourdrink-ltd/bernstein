"""Result receipt bundle: the #3870 test matrix, offline and side-effect free.

    1. round-trip create-then-verify
    2. patch tamper detected
    3. gate-log tamper detected
    4. wrong-key signature rejected
    5. serialization determinism (byte-identical re-serialize)

plus chain continuity and the field-level-error guarantee the acceptance
criteria call for.
"""

from __future__ import annotations

import base64
import builtins
import json
import socket
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein.core.security.audit_dsse import export_public_key_pem, keyid_from_public_key
from bernstein.core.security.result_receipt_bundle import (
    GENESIS_ANCHOR,
    BundleVerification,
    ChainLink,
    GateResult,
    ResultBundle,
    TaskRef,
    build_result_bundle,
    bundle_with_manifest_digest,
    parse_bundle,
    verify_result_bundle,
)
from bernstein.core.volunteer.manifest import VolunteerManifest, load_manifest

#: A valid manifest document, mirroring tests/unit/volunteer/test_volunteer_manifest.py.
#: Real, because a digest over a made-up policy proves nothing about the field
#: this file exists to check (#3911).
_VALID_MANIFEST: dict[str, Any] = {
    "version": 1,
    "license": "Apache-2.0",
    "gates": [["uv", "run", "pytest", "-q"], ["uv", "run", "ruff", "check", "."]],
    "allowed_paths": ["src/**", "tests/**"],
    "egress_allowlist": ["pypi.org"],
    "sandbox": "microvm",
    "max_wall_clock_minutes": 30,
    "task_label": "volunteer-ok",
    "local_ok": True,
}


def _manifest(**overrides: Any) -> VolunteerManifest:
    """A real, validated manifest -- never a hand-written hex string."""
    return load_manifest(json.dumps({**_VALID_MANIFEST, **overrides}))


def _key(seed_byte: int = 7) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(bytes([seed_byte]) * 32)


def _bundle(
    key: Ed25519PrivateKey,
    *,
    patch: str = "diff --git a/x b/x\n+hello\n",
    manifest_sha256: str | None = None,
) -> ResultBundle:
    pub = key.public_key()
    return ResultBundle(
        task=TaskRef(repo="sipyourdrink-ltd/bernstein", commit_sha="abc123def456", issue_number=3870),
        patch=patch,
        gates=(
            GateResult(command="pytest -q", exit_code=0, log="42 passed in 3.1s\n"),
            GateResult(command="ruff check", exit_code=0, log="All checks passed!\n"),
        ),
        manifest_sha256=_manifest().digest if manifest_sha256 is None else manifest_sha256,
        adapter_id="adapter.default.v3",
        model_id="claude-x",
        sandbox_profile="restricted-net-off",
        selection_receipt="sel-receipt-9f2",
        created_at="2026-08-15T00:00:00Z",
        worker_keyid=keyid_from_public_key(pub),
        worker_public_key_pem=export_public_key_pem(pub).decode("ascii"),
        chain=ChainLink(anchor=GENESIS_ANCHOR, length=1),
    )


def test_roundtrip_create_then_verify():
    key = _key()
    env = build_result_bundle(_bundle(key), signing_key=key)
    v = verify_result_bundle(env, key.public_key())
    assert v.ok, v.errors
    assert v.bundle["task"]["issue_number"] == 3870
    assert v.digest and v.keyid == keyid_from_public_key(key.public_key())


def test_patch_tamper_detected_field_level():
    key = _key()
    env = build_result_bundle(_bundle(key), signing_key=key)
    # tamper the embedded patch inside the signed payload, then re-encode the
    # payload without re-signing -- the attacker cannot forge the signature.
    payload = json.loads(base64.b64decode(env.payload_b64))
    payload["predicate"]["bundle"]["patch"] += "\n+malicious\n"
    tampered = env.__class__(
        payload_type=env.payload_type,
        payload_b64=base64.b64encode(json.dumps(payload).encode()).decode(),
        signatures=env.signatures,
    )
    v = verify_result_bundle(tampered, key.public_key())
    assert not v.ok
    # signature breaks first (whole-payload integrity); the envelope error names it
    assert any("envelope" in e.field or "patch" in e.field for e in v.errors)


def test_patch_field_hash_mismatch_is_field_level():
    # a bundle whose patch_sha256 disagrees with its patch (no signature involved)
    # must produce a patch-field error, proving the field-level check itself works.
    key = _key()
    b = _bundle(key)
    bundle_dict = b.to_dict()
    bundle_dict["patch"] = bundle_dict["patch"] + "tampered"
    # forge a self-consistent envelope over the tampered dict but keep the OLD
    # patch_sha256, isolating the field check.
    bundle_dict["patch_sha256"] = b.patch_sha256  # stale hash
    stmt_payload = _sign_dict(key, bundle_dict)
    v = verify_result_bundle(stmt_payload, key.public_key())
    assert not v.ok
    assert any(e.field == "patch" for e in v.errors)


def _sign_dict(key, bundle_dict):
    """Build a validly-signed envelope directly over a (possibly hand-edited)
    bundle dict, so a test can exercise the field checks past the signature."""
    import base64 as b64

    from bernstein.core.security import audit_dsse as ad
    from bernstein.core.security import result_receipt_bundle as rb

    canon = rb.canonical_bytes(bundle_dict)
    subject = ad.Subject(name="t.json", digest={"sha256": rb._sha256_hex(canon)})
    statement = ad.Statement(
        subjects=[subject],
        predicate_type=rb.RESULT_RECEIPT_PREDICATE_TYPE,
        predicate={
            "schema_version": rb.BUNDLE_SCHEMA_VERSION,
            "bundle_kind": "result-receipt",
            "bundle": bundle_dict,
            # ``.get`` rather than ``[...]``: #4050 needs to sign a bundle with
            # no chain key at all, and the predicate mirror of a missing field
            # is the same missing field. Identical for every existing caller.
            "chain": bundle_dict.get("chain"),
        },
    )
    payload = rb.canonical_bytes(statement.to_dict())
    sig = key.sign(ad.pae(ad.DSSE_PAYLOAD_TYPE, payload))
    return ad.Envelope(
        payload_type=ad.DSSE_PAYLOAD_TYPE,
        payload_b64=b64.b64encode(payload).decode(),
        signatures=[ad.Signature(keyid=ad.keyid_from_public_key(key.public_key()), sig=b64.b64encode(sig).decode())],
    )


def test_gate_log_tamper_detected_field_level():
    key = _key()
    b = _bundle(key)
    bundle_dict = b.to_dict()
    bundle_dict["gates"][0]["log"] = "42 passed in 3.1s\n(secretly 0 passed)\n"  # stale log_sha256
    env = _sign_dict(key, bundle_dict)
    v = verify_result_bundle(env, key.public_key())
    assert not v.ok
    assert any(e.field == "gates[0].log" for e in v.errors)


def test_wrong_key_signature_rejected():
    key = _key(7)
    env = build_result_bundle(_bundle(key), signing_key=key)
    wrong = _key(9).public_key()
    v = verify_result_bundle(env, wrong)
    assert not v.ok
    assert any(e.field == "envelope" for e in v.errors)


def test_serialization_determinism():
    key = _key()
    b = _bundle(key)
    # Ed25519 is deterministic -> the whole envelope is byte-identical.
    e1 = build_result_bundle(b, signing_key=key)
    e2 = build_result_bundle(b, signing_key=key)
    assert e1.to_json() == e2.to_json()
    # and the bundle's own canonical bytes re-serialize identically
    assert b.canonical_bytes() == b.canonical_bytes()


def test_chain_continuity():
    key = _key()
    first = _bundle(key)
    env1 = build_result_bundle(first, signing_key=key)
    v1 = verify_result_bundle(env1, key.public_key())
    assert v1.ok

    # the successor links to the first bundle's digest
    pub = key.public_key()
    second = ResultBundle(
        task=first.task,
        patch="diff --git a/y b/y\n+two\n",
        gates=first.gates,
        manifest_sha256=_manifest(max_wall_clock_minutes=45).digest,
        adapter_id=first.adapter_id,
        model_id=first.model_id,
        sandbox_profile=first.sandbox_profile,
        selection_receipt="sel-2",
        created_at="2026-08-15T01:00:00Z",
        worker_keyid=first.worker_keyid,
        worker_public_key_pem=first.worker_public_key_pem,
        chain=ChainLink(anchor=first.digest, length=2),
    )
    env2 = build_result_bundle(second, signing_key=key)
    good = verify_result_bundle(env2, pub, expected_prev_digest=first.digest)
    assert good.ok, good.errors
    # a broken link is a field-level error
    bad = verify_result_bundle(env2, pub, expected_prev_digest="deadbeef")
    assert not bad.ok
    assert any(e.field == "chain.anchor" for e in bad.errors)


def test_verify_is_offline_and_pure(tmp_path):
    # verification touches no network and returns the same result twice.
    key = _key()
    env = build_result_bundle(_bundle(key), signing_key=key)
    a = verify_result_bundle(env, key.public_key())
    b = verify_result_bundle(env, key.public_key())
    assert a.ok and b.ok and a.digest == b.digest


def test_parse_roundtrip():
    key = _key()
    env = build_result_bundle(_bundle(key), signing_key=key)
    reparsed = parse_bundle(json.loads(env.to_json()))
    v = verify_result_bundle(reparsed, key.public_key())
    assert v.ok


# ---------------------------------------------------------------------------
# #3911: the manifest digest is derived, and its check is visible in the verdict
#
# Every manifest below is a real, validated VolunteerManifest whose ``.digest``
# is computed by the one producer. Two hand-written hex strings would pass all
# of these while proving nothing -- that is the exact shape of proof the
# ``"0" * 64`` fixtures were.
# ---------------------------------------------------------------------------


def test_a_bundle_from_one_manifest_fails_against_a_different_manifest_digest():
    key = _key()
    manifest_a = _manifest()
    manifest_b = _manifest(max_wall_clock_minutes=45)
    assert manifest_a.digest != manifest_b.digest, "the two fixtures must be genuinely different policies"

    env = build_result_bundle(bundle_with_manifest_digest(_bundle(key), manifest_a), signing_key=key)

    good = verify_result_bundle(env, key.public_key(), expected_manifest_sha256=manifest_a.digest)
    assert good.ok, good.errors

    bad = verify_result_bundle(env, key.public_key(), expected_manifest_sha256=manifest_b.digest)
    assert not bad.ok
    assert [e.field for e in bad.errors] == ["manifest_sha256"]
    # the failure names its reference, so the verdict alone says what was expected
    assert manifest_b.digest in bad.errors[0].message


def test_a_verdict_distinguishes_checked_from_merely_carried():
    """An unchecked field reported as verified is the whole bug, restated."""
    key = _key()
    manifest = _manifest()
    env = build_result_bundle(bundle_with_manifest_digest(_bundle(key), manifest), signing_key=key)

    unchecked = verify_result_bundle(env, key.public_key())
    checked = verify_result_bundle(env, key.public_key(), expected_manifest_sha256=manifest.digest)

    # Both succeed -- not supplying the digest is not a failure, it is a
    # narrower question -- so ``ok`` cannot be what tells them apart.
    assert unchecked.ok and checked.ok
    assert unchecked.errors == () and checked.errors == ()
    assert unchecked.manifest_digest_checked is False
    assert checked.manifest_digest_checked is True
    assert unchecked != checked, "the two verdicts must not be indistinguishable objects"


def test_a_failed_signature_does_not_report_the_manifest_digest_as_checked():
    """The early return happens before the comparison, so it never ran.

    Not in #3911's matrix, and it is the state where the obvious
    implementation -- recording ``expected is not None`` at the top of the
    function -- reports a check that provably did not happen, on the one
    verdict a caller is most likely to be reading carefully.
    """
    key = _key()
    manifest = _manifest()
    env = build_result_bundle(bundle_with_manifest_digest(_bundle(key), manifest), signing_key=key)

    wrong_key = _key(9).public_key()
    v = verify_result_bundle(env, wrong_key, expected_manifest_sha256=manifest.digest)

    assert not v.ok
    assert [e.field for e in v.errors] == ["envelope"]
    assert v.manifest_digest_checked is False


def test_the_builder_derives_the_digest_rather_than_trusting_its_caller():
    key = _key()
    manifest = _manifest()
    fake = "wrong" + "0" * 59

    derived = bundle_with_manifest_digest(_bundle(key, manifest_sha256=fake), manifest)

    assert derived.manifest_sha256 == manifest.digest
    assert fake not in derived.canonical_bytes().decode("utf-8"), "the caller's value must not survive anywhere"


def test_reordering_manifest_keys_does_not_change_the_bundles_digest():
    """Canonicalisation is manifest.py's job; this pins that the bundle inherits it."""
    key = _key()
    forward = load_manifest(json.dumps(dict(sorted(_VALID_MANIFEST.items()))))
    reversed_ = load_manifest(json.dumps(dict(sorted(_VALID_MANIFEST.items(), reverse=True))))
    assert forward.digest == reversed_.digest

    a = bundle_with_manifest_digest(_bundle(key), forward)
    b = bundle_with_manifest_digest(_bundle(key), reversed_)
    assert a.manifest_sha256 == b.manifest_sha256
    assert a.canonical_bytes() == b.canonical_bytes()


def test_verification_opens_no_files_and_no_sockets(monkeypatch: pytest.MonkeyPatch):
    """The new comparison is string equality, not a late read of the manifest."""
    key = _key()
    manifest = _manifest()
    env = build_result_bundle(bundle_with_manifest_digest(_bundle(key), manifest), signing_key=key)

    def _no_open(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"verification opened a file: {args!r}")

    def _no_connect(self: Any, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"verification opened a socket: {args!r}")

    monkeypatch.setattr(builtins, "open", _no_open)
    monkeypatch.setattr(socket.socket, "connect", _no_connect)

    v = verify_result_bundle(env, key.public_key(), expected_manifest_sha256=manifest.digest)
    assert v.ok and v.manifest_digest_checked


# ---------------------------------------------------------------------------
# #4050: the continuity check is visible in the verdict too, and it is recorded
# where the comparison happens rather than where the argument arrives.
#
# The distinction matters here in a way it did not for the manifest digest:
# step 7 is unconditional, so reaching the end of the function means it ran,
# while step 6 is the last arm of an elif chain that a malformed link
# short-circuits. Every test below that pins "not checked" pins a state in
# which the caller *did* supply a digest.
# ---------------------------------------------------------------------------


def _successor(key: Ed25519PrivateKey, first: ResultBundle, *, anchor: str, length: int = 2) -> ResultBundle:
    """A second bundle in a chain, linking to ``anchor``."""
    return ResultBundle(
        task=first.task,
        patch="diff --git a/y b/y\n+two\n",
        gates=first.gates,
        manifest_sha256=first.manifest_sha256,
        adapter_id=first.adapter_id,
        model_id=first.model_id,
        sandbox_profile=first.sandbox_profile,
        selection_receipt="sel-4050",
        created_at="2026-08-17T00:00:00Z",
        worker_keyid=first.worker_keyid,
        worker_public_key_pem=first.worker_public_key_pem,
        chain=ChainLink(anchor=anchor, length=length),
    )


def test_a_verified_chain_anchor_and_an_unasked_about_one_are_different_verdicts():
    """The defect #4050 names, stated as the test that closes it."""
    key = _key()
    pub = key.public_key()
    first = _bundle(key)
    env = build_result_bundle(_successor(key, first, anchor=first.digest), signing_key=key)

    unchecked = verify_result_bundle(env, pub)
    checked = verify_result_bundle(env, pub, expected_prev_digest=first.digest)

    # Both succeed, so ``ok`` cannot be what tells them apart -- not asking is a
    # narrower question, not a failure.
    assert unchecked.ok and checked.ok
    assert unchecked.errors == () and checked.errors == ()
    assert unchecked.prev_digest_checked is False
    assert checked.prev_digest_checked is True
    assert unchecked != checked, "the two verdicts must not be indistinguishable objects"


def test_a_malformed_chain_link_does_not_report_continuity_as_checked():
    """Asked, and the anchor was still never looked at.

    ``prev_digest_checked = expected_prev_digest is not None`` -- correct for
    the manifest field -- is wrong here: the comparison is the third arm of an
    elif chain, so a bundle with no anchor at all would be reported as
    continuity-checked by that implementation.
    """
    key = _key()
    pub = key.public_key()
    first = _bundle(key)

    for label, mutate in (
        ("anchor missing", lambda d: d["chain"].pop("anchor")),
        ("length missing", lambda d: d["chain"].pop("length")),
        ("chain absent", lambda d: d.pop("chain")),
        ("length not an int", lambda d: d["chain"].__setitem__("length", "2")),
        ("length below one", lambda d: d["chain"].__setitem__("length", 0)),
    ):
        bundle_dict = _successor(key, first, anchor=first.digest).to_dict()
        mutate(bundle_dict)
        v = verify_result_bundle(_sign_dict(key, bundle_dict), pub, expected_prev_digest=first.digest)

        assert not v.ok, label
        assert v.prev_digest_checked is False, label
        # and the verdict says *why* it could not answer, which is what makes a
        # plain bool sufficient: "asked but unanswerable" is this state, and it
        # carries a chain-shaped error that "never asked" cannot.
        assert [e.field for e in v.errors] == ["chain"] or [e.field for e in v.errors] == ["chain.length"], label


def test_a_failed_signature_does_not_report_continuity_as_checked():
    """The early return happens before step 6, so nothing in it ran."""
    key = _key()
    first = _bundle(key)
    env = build_result_bundle(_successor(key, first, anchor=first.digest), signing_key=key)

    v = verify_result_bundle(env, _key(9).public_key(), expected_prev_digest=first.digest)

    assert not v.ok
    assert [e.field for e in v.errors] == ["envelope"]
    assert v.prev_digest_checked is False


def test_a_broken_link_still_counts_as_checked():
    """A comparison that ran and said no is a comparison that ran.

    The flag records whether the question was answered, not whether the answer
    was yes -- conflating the two would make ``prev_digest_checked`` a second,
    lossier copy of ``ok``.
    """
    key = _key()
    pub = key.public_key()
    first = _bundle(key)
    env = build_result_bundle(_successor(key, first, anchor=first.digest), signing_key=key)

    v = verify_result_bundle(env, pub, expected_prev_digest="deadbeef")

    assert not v.ok
    assert [e.field for e in v.errors] == ["chain.anchor"]
    assert v.prev_digest_checked is True


def test_the_two_checked_flags_are_independent():
    """All four combinations, asserted separately.

    A refactor that collapses these into one "something was checked" flag has
    to fail here. Asserting only the diagonal (both true / both false) would
    let it through.
    """
    key = _key()
    pub = key.public_key()
    manifest = _manifest()
    first = _bundle(key)
    successor = _successor(key, first, anchor=first.digest)
    env = build_result_bundle(bundle_with_manifest_digest(successor, manifest), signing_key=key)

    neither = verify_result_bundle(env, pub)
    prev_only = verify_result_bundle(env, pub, expected_prev_digest=first.digest)
    manifest_only = verify_result_bundle(env, pub, expected_manifest_sha256=manifest.digest)
    both = verify_result_bundle(env, pub, expected_prev_digest=first.digest, expected_manifest_sha256=manifest.digest)

    assert neither.prev_digest_checked is False
    assert neither.manifest_digest_checked is False

    assert prev_only.prev_digest_checked is True
    assert prev_only.manifest_digest_checked is False

    assert manifest_only.prev_digest_checked is False
    assert manifest_only.manifest_digest_checked is True

    assert both.prev_digest_checked is True
    assert both.manifest_digest_checked is True

    # ...and the off-diagonal pair is what a single collapsed flag cannot
    # represent: one flag has no way to hold "one of the two ran". Pin that all
    # four verdicts are pairwise distinguishable objects. (``BundleVerification``
    # is frozen but carries a dict, so it is unhashable -- compare pairwise
    # rather than through a set.)
    verdicts = [neither, prev_only, manifest_only, both]
    for i, a in enumerate(verdicts):
        for b in verdicts[i + 1 :]:
            assert a != b


def test_the_manifest_flag_is_unaffected_by_a_malformed_chain():
    """Step 7 runs after step 6 and is not inside its elif chain.

    Pins the asymmetry the docstring claims: the same malformed link that
    suppresses ``prev_digest_checked`` must not suppress the manifest one.
    """
    key = _key()
    manifest = _manifest()
    first = _bundle(key)
    successor = bundle_with_manifest_digest(_successor(key, first, anchor=first.digest), manifest)

    bundle_dict = successor.to_dict()
    bundle_dict["chain"].pop("anchor")
    v = verify_result_bundle(
        _sign_dict(key, bundle_dict),
        key.public_key(),
        expected_prev_digest=first.digest,
        expected_manifest_sha256=manifest.digest,
    )

    assert not v.ok
    assert [e.field for e in v.errors] == ["chain"]
    assert v.prev_digest_checked is False
    assert v.manifest_digest_checked is True


def test_the_flag_defaults_false_for_every_existing_caller():
    """Additive: a verdict nobody asked a continuity question of reads False."""
    key = _key()
    env = build_result_bundle(_bundle(key), signing_key=key)

    assert verify_result_bundle(env, key.public_key()).prev_digest_checked is False
    assert BundleVerification(ok=True).prev_digest_checked is False


# --------------------------------------------------------------------------- #
# #4054: the chain-shape check must refuse rather than raise, and a boolean is
# not a length.
#
# ``verify_result_bundle`` exists so that untrusted input produces a verdict
# naming the field that diverged. Step 6 broke that in both directions:
# ``bundle_dict.get("chain", {})`` defends against the key being absent, not
# against it holding the wrong type, and ``isinstance(True, int)`` let the JSON
# boolean ``true`` through as a chain length.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("chain", "type_name"),
    [
        ("anchor and length", "str"),
        (["anchor", "length"], "list"),
        (None, "NoneType"),
        (42, "int"),
        # The two below reach the membership test rather than the raise, because
        # ``in`` silently means substring on a str and element-of on a list. They
        # are refused today with "missing anchor or length" -- an accurate-looking
        # message about the wrong problem -- so the guard has to fire on the type
        # before the content, not merely somewhere.
        ("", "str"),
        ([], "list"),
    ],
    ids=["str", "list", "null", "int", "str-without-the-words", "empty-list"],
)
def test_a_non_mapping_chain_is_refused_rather_than_raised(chain: Any, type_name: str):
    """The contract, on the boundary that has to hold it.

    Reachable from ``bernstein receipt verify <bundle>`` with no ``--pubkey``:
    a self-signed bundle clears the signature step on its own embedded key, so
    what an operator got was a traceback where the documented behaviour is
    ``✗`` and exit 1.

    No ``pytest.raises`` here on purpose -- the assertion is that the call
    returns, and a raise fails the test by escaping it.
    """
    key = _key()
    bundle_dict = _bundle(key).to_dict()
    bundle_dict["chain"] = chain

    v = verify_result_bundle(_sign_dict(key, bundle_dict), key.public_key())

    assert v.ok is False
    assert [e.field for e in v.errors] == ["chain"]
    assert v.errors[0].message == f"expected an object, got {type_name}"


def test_a_non_mapping_chain_reports_the_field_that_diverged_not_a_path_that_does_not_exist():
    """The naming decision, pinned.

    A missing anchor and a ``chain`` that is a string are both errors on
    ``chain`` and are told apart by the message, not by the field. Every field
    name this function emits is a JSON path into the bundle -- ``patch``,
    ``gates[0].log``, ``chain.anchor``, ``manifest_sha256`` -- and there is no
    ``chain.anchor`` to name when ``chain`` is a string.
    """
    key = _key()
    missing = _bundle(key).to_dict()
    missing["chain"].pop("length")
    wrong_type = _bundle(key).to_dict()
    wrong_type["chain"] = "anchor and length"

    v_missing = verify_result_bundle(_sign_dict(key, missing), key.public_key())
    v_wrong = verify_result_bundle(_sign_dict(key, wrong_type), key.public_key())

    assert [e.field for e in v_missing.errors] == [e.field for e in v_wrong.errors] == ["chain"]
    assert v_missing.errors[0].message != v_wrong.errors[0].message


def test_a_non_mapping_chain_leaves_both_checked_flags_where_they_were():
    """The out-of-scope guard from #4054, on the new arm.

    The comparison is skipped, so continuity was asked and never answered; the
    manifest check happens below and is unaffected by the shape of ``chain``.
    """
    key = _key()
    manifest = _manifest()
    bundle_dict = bundle_with_manifest_digest(_bundle(key), manifest).to_dict()
    bundle_dict["chain"] = ["anchor", "length"]

    v = verify_result_bundle(
        _sign_dict(key, bundle_dict),
        key.public_key(),
        expected_prev_digest="whatever",
        expected_manifest_sha256=manifest.digest,
    )

    assert v.ok is False
    assert v.prev_digest_checked is False
    assert v.manifest_digest_checked is True


@pytest.mark.parametrize("length", [True, False], ids=["true", "false"])
def test_a_boolean_chain_length_is_not_a_length(length: bool):
    """``isinstance(True, int)`` is True and ``True >= 1``, so ``true`` verified.

    ``false`` was already refused by the ``< 1`` comparison, which is why only
    ``true`` was the hole -- both are asserted so a future rewrite of the guard
    cannot fix one by losing the other. Same idiom as ``_load_version`` in
    ``bernstein.core.volunteer.manifest``, which rejects a bool version.
    """
    key = _key()
    bundle_dict = _bundle(key).to_dict()
    bundle_dict["chain"]["length"] = length

    v = verify_result_bundle(_sign_dict(key, bundle_dict), key.public_key())

    assert v.ok is False
    assert [e.field for e in v.errors] == ["chain.length"]


def test_a_real_integer_length_still_verifies():
    """The control: the guard must refuse two shapes and nothing else."""
    key = _key()
    first = _bundle(key)
    third = _successor(key, first, anchor=first.digest, length=3)

    v = verify_result_bundle(
        build_result_bundle(third, signing_key=key),
        key.public_key(),
        expected_prev_digest=first.digest,
    )

    assert v.ok is True, [str(e) for e in v.errors]
    assert v.prev_digest_checked is True
