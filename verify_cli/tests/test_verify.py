"""Unit tests for bernstein_verify.verify.

These tests cross-import `bernstein` at TEST scope to prove byte-for-byte
compatibility of our re-implementation. The package under test
(`bernstein_verify`) never imports `bernstein` itself - see
test_no_bernstein_install.py for the install-isolation proof.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from dataclasses import asdict
from pathlib import Path

# Cross-test: original implementation
from bernstein.core.lineage.entry import LineageEntry
from bernstein.core.lineage.entry import canonicalise as bernstein_canonicalise
from bernstein.core.lineage.identity import (
    generate_keypair as bernstein_generate_keypair,
)
from bernstein.core.lineage.identity import (
    sign_detached as bernstein_sign_detached,
)

from bernstein_verify.verify import (
    VerifyResult,
    jcs_canonicalise,
    verify_jws_detached,
    verify_pack,
    walk_chain,
)

# ---------- jcs_canonicalise: byte-equality with bernstein ----------


def _sample_entry_dict(**overrides):
    base = dict(
        v=1,
        artefact_path="src/foo.py",
        artefact_kind="file",
        content_hash="sha256:" + "a" * 64,
        parent_hashes=[],
        agent_id="agent:claude-worker-3",
        agent_card_kid="key-001",
        tool_call_id="tc-7f3a",
        span_id="00f067aa0ba902b7",
        ts_ns=1_715_600_000_000_000_000,
        operator_hmac="deadbeef" * 8,
    )
    base.update(overrides)
    return base


def test_jcs_canonicalise_byte_equal_to_bernstein_simple():
    d = _sample_entry_dict()
    entry = LineageEntry(**d)
    assert jcs_canonicalise(asdict(entry)) == bernstein_canonicalise(entry)


def test_jcs_canonicalise_byte_equal_with_parents():
    d = _sample_entry_dict(parent_hashes=["sha256:" + "1" * 64, "sha256:" + "2" * 64])
    entry = LineageEntry(**d)
    assert jcs_canonicalise(asdict(entry)) == bernstein_canonicalise(entry)


def test_jcs_canonicalise_byte_equal_unicode():
    d = _sample_entry_dict(artefact_path="src/файл.py", agent_id="agent:ünïcødé")
    entry = LineageEntry(**d)
    assert jcs_canonicalise(asdict(entry)) == bernstein_canonicalise(entry)


def test_jcs_canonicalise_independent_of_input_order():
    a = {"b": 1, "a": 2, "c": 3}
    b = {"c": 3, "a": 2, "b": 1}
    assert jcs_canonicalise(a) == jcs_canonicalise(b)


def test_jcs_canonicalise_no_whitespace():
    out = jcs_canonicalise(_sample_entry_dict())
    assert b": " not in out
    assert b", " not in out
    assert b"\n" not in out


def test_jcs_canonicalise_keys_sorted():
    out = jcs_canonicalise(_sample_entry_dict())
    assert out.startswith(b'{"agent_card_kid":')


# ---------- verify_jws_detached: bernstein-signed JWS verifies here ----------


def test_verify_jws_detached_happy_path():
    priv, pub = bernstein_generate_keypair()
    payload = b"some canonical bytes"
    jws = bernstein_sign_detached(payload, priv, kid="k1")
    assert verify_jws_detached(payload, jws, pub, expected_kid="k1") is True


def test_verify_jws_detached_tamper_payload_fails():
    priv, pub = bernstein_generate_keypair()
    payload = b"original"
    jws = bernstein_sign_detached(payload, priv, kid="k1")
    assert verify_jws_detached(b"tampered", jws, pub, expected_kid="k1") is False


def test_verify_jws_detached_wrong_kid_fails():
    priv, pub = bernstein_generate_keypair()
    jws = bernstein_sign_detached(b"x", priv, kid="k1")
    assert verify_jws_detached(b"x", jws, pub, expected_kid="WRONG") is False


def test_verify_jws_detached_wrong_key_fails():
    priv_a, _ = bernstein_generate_keypair()
    _, pub_b = bernstein_generate_keypair()
    jws = bernstein_sign_detached(b"x", priv_a, kid="k1")
    assert verify_jws_detached(b"x", jws, pub_b, expected_kid="k1") is False


def test_verify_jws_detached_malformed_input_returns_false_never_raises():
    _, pub = bernstein_generate_keypair()
    bad_inputs = [
        "",
        "not-a-jws",
        "a.b.c.d",
        "...",
        "a.b.c",  # 3 dot-separated but middle non-empty
        "header..signature.extra",
    ]
    for jws in bad_inputs:
        assert verify_jws_detached(b"x", jws, pub, expected_kid="k1") is False


def test_verify_jws_detached_malformed_pem_returns_false():
    priv, _ = bernstein_generate_keypair()
    jws = bernstein_sign_detached(b"x", priv, kid="k1")
    assert verify_jws_detached(b"x", jws, "not a pem", expected_kid="k1") is False


def test_verify_jws_detached_wrong_alg_fails():
    # Forge a JWS header with alg=HS256
    import base64

    priv, pub = bernstein_generate_keypair()
    real_jws = bernstein_sign_detached(b"x", priv, kid="k1")
    _real_protected, _, sig = real_jws.split(".", maxsplit=2)
    bad_header = {"alg": "HS256", "kid": "k1", "b64": False, "crit": ["b64"]}
    bad_protected = (
        base64.urlsafe_b64encode(
            json.dumps(bad_header, separators=(",", ":"), sort_keys=True).encode()
        )
        .rstrip(b"=")
        .decode("ascii")
    )
    forged = bad_protected + ".." + sig
    assert verify_jws_detached(b"x", forged, pub, expected_kid="k1") is False


# ---------- walk_chain ----------


def _mk_entry(content: str, parents: list[str], agent: str = "agent:a") -> dict:
    return _sample_entry_dict(
        content_hash="sha256:" + hashlib.sha256(content.encode()).hexdigest(),
        parent_hashes=parents,
        agent_id=agent,
    )


def _entry_h(d: dict) -> str:
    return "sha256:" + hashlib.sha256(jcs_canonicalise(d)).hexdigest()


def test_walk_chain_happy_path():
    g = _mk_entry("v1", [])
    g_h = _entry_h(g)
    c1 = _mk_entry("v2", [g_h])
    c1_h = _entry_h(c1)
    c2 = _mk_entry("v3", [c1_h])
    ok, errors = walk_chain([g, c1, c2])
    assert ok is True
    assert errors == []


def test_walk_chain_genesis_missing_parents_ok():
    g = _mk_entry("v1", [])
    ok, errors = walk_chain([g])
    assert ok is True
    assert errors == []


def test_walk_chain_orphan_parent_detected():
    g = _mk_entry("v1", ["sha256:" + "f" * 64])
    ok, errors = walk_chain([g])
    assert ok is False
    assert any("orphan" in e.lower() or "unknown parent" in e.lower() for e in errors)


def test_walk_chain_duplicate_entry_detected():
    g = _mk_entry("v1", [])
    ok, errors = walk_chain([g, g])
    assert ok is False
    assert any("duplicate" in e.lower() for e in errors)


def test_walk_chain_merge_entry_two_parents_ok():
    g = _mk_entry("v1", [])
    g_h = _entry_h(g)
    a = _mk_entry("v2a", [g_h], agent="agent:a")
    b = _mk_entry("v2b", [g_h], agent="agent:b")
    a_h, b_h = _entry_h(a), _entry_h(b)
    m = _mk_entry("v3", [a_h, b_h], agent="agent:steward")
    ok, errors = walk_chain([g, a, b, m])
    assert ok is True, errors


def test_walk_chain_out_of_order_parents_still_ok():
    """walk_chain must accept any topological order - entries can arrive
    in any order in the log; what matters is that every parent exists."""
    g = _mk_entry("v1", [])
    g_h = _entry_h(g)
    c = _mk_entry("v2", [g_h])
    ok, errors = walk_chain([c, g])  # child before parent
    assert ok is True, errors


# ---------- verify_pack ----------


def _build_pack(
    tmp_path: Path,
    *,
    tamper_log: bool = False,
    drop_jws: bool = False,
    wrong_kid_card: bool = False,
) -> Path:
    """Build a minimal compliance pack for testing."""
    priv, pub = bernstein_generate_keypair()
    kid = "k1"
    agent_id = "agent:t"

    # Genesis entry
    entry = LineageEntry(
        v=1,
        artefact_path="src/foo.py",
        artefact_kind="file",
        content_hash="sha256:" + "a" * 64,
        parent_hashes=[],
        agent_id=agent_id,
        agent_card_kid=kid,
        tool_call_id="tc-1",
        span_id="00f067aa0ba902b7",
        ts_ns=1_715_600_000_000_000_000,
        operator_hmac="deadbeef" * 8,
    )
    payload = bernstein_canonicalise(entry)
    jws = bernstein_sign_detached(payload, priv, kid=kid)
    entry_hash = "sha256:" + hashlib.sha256(payload).hexdigest()

    card = {
        "agent_id": agent_id,
        "kid": kid if not wrong_kid_card else "WRONG",
        "public_key_pem": pub,
        "protocol_version": "a2a/1.0",
    }

    log_line = json.dumps(asdict(entry), separators=(",", ":"), sort_keys=True)
    if tamper_log:
        log_line = log_line.replace("a" * 64, "b" + "a" * 63)

    bundle = tmp_path / "bundle.zip"
    with zipfile.ZipFile(bundle, "w") as z:
        z.writestr("lineage-log.jsonl", log_line + "\n")
        if not drop_jws:
            z.writestr(f"signatures/{entry_hash}.jws", jws)
        z.writestr(f"agent-cards/{agent_id}.json", json.dumps(card))
    return bundle


def test_verify_pack_happy_path(tmp_path):
    bundle = _build_pack(tmp_path)
    result = verify_pack(bundle)
    assert isinstance(result, VerifyResult)
    assert result.ok is True, result.errors


def test_verify_pack_tampered_log_fails(tmp_path):
    bundle = _build_pack(tmp_path, tamper_log=True)
    result = verify_pack(bundle)
    assert result.ok is False
    # The flipped content_hash byte means entry-hash changes, signature lookup
    # fails, AND if found would fail crypto verify. Either way: ok=False.
    assert result.errors


def test_verify_pack_missing_jws_fails(tmp_path):
    bundle = _build_pack(tmp_path, drop_jws=True)
    result = verify_pack(bundle)
    assert result.ok is False
    assert any("signature" in e.lower() or "jws" in e.lower() for e in result.errors)


def test_verify_pack_wrong_kid_card_fails(tmp_path):
    bundle = _build_pack(tmp_path, wrong_kid_card=True)
    result = verify_pack(bundle)
    assert result.ok is False


def test_verify_pack_missing_log_fails(tmp_path):
    bundle = tmp_path / "empty.zip"
    with zipfile.ZipFile(bundle, "w") as z:
        z.writestr("README.md", "hi")
    result = verify_pack(bundle)
    assert result.ok is False


def test_verify_pack_not_a_zip_fails(tmp_path):
    bundle = tmp_path / "not-a-zip.txt"
    bundle.write_text("hello")
    result = verify_pack(bundle)
    assert result.ok is False


def test_verify_pack_zip_slip_blocked(tmp_path):
    """A malicious zip with `../../../etc/passwd` entries must not write
    outside the extraction sandbox. verify_pack reads from the zip in-memory,
    so this is a defence-in-depth check that we never call extractall().
    """
    bundle = tmp_path / "evil.zip"
    with zipfile.ZipFile(bundle, "w") as z:
        z.writestr("../../../etc/passwd", "rooted")
        z.writestr("lineage-log.jsonl", "")
    # Must not raise, must not write outside tmp_path.
    sentinel = Path("/etc/passwd-bernstein-test")
    assert not sentinel.exists()
    verify_pack(bundle)  # may pass or fail; must NOT escape sandbox
    assert not sentinel.exists()


# ---------- verify_pack: byte-binding for v2 packs (issue #1871) ----------


def _build_pack_bytes(
    tmp_path: Path,
    *,
    log_bytes: bytes,
    pack_format_version: int | None = 2,
    name: str = "bundle.zip",
) -> Path:
    """Build a compliance pack with caller-supplied raw log bytes.

    The signature is computed over the *canonical* entry (what the real
    writer signs); ``log_bytes`` is whatever the caller wants on disk, so a
    test can write a value-preserving byte tamper (reordered keys, spaced
    separators, flipped or missing terminator) and assert the verifier still
    rejects it.

    A ``pack-manifest.json`` is written carrying ``pack_format_version`` so
    the verifier can dispatch its byte-binding rule. ``None`` omits the field
    entirely (a genuine pre-fix / legacy pack shape).
    """
    priv, pub = bernstein_generate_keypair()
    kid = "k1"
    agent_id = "agent:t"

    entry = LineageEntry(
        v=1,
        artefact_path="src/foo.py",
        artefact_kind="file",
        content_hash="sha256:" + "a" * 64,
        parent_hashes=[],
        agent_id=agent_id,
        agent_card_kid=kid,
        tool_call_id="tc-1",
        span_id="00f067aa0ba902b7",
        ts_ns=1_715_600_000_000_000_000,
        operator_hmac="deadbeef" * 8,
    )
    payload = bernstein_canonicalise(entry)
    jws = bernstein_sign_detached(payload, priv, kid=kid)
    entry_hash = "sha256:" + hashlib.sha256(payload).hexdigest()
    card = {
        "agent_id": agent_id,
        "kid": kid,
        "public_key_pem": pub,
        "protocol_version": "a2a/1.0",
    }
    manifest: dict = {"schema": "https://bernstein.run/compliance/pack-manifest/v1"}
    if pack_format_version is not None:
        manifest["pack_format_version"] = pack_format_version

    bundle = tmp_path / name
    with zipfile.ZipFile(bundle, "w") as z:
        z.writestr("lineage-log.jsonl", log_bytes)
        z.writestr(f"signatures/{entry_hash}.jws", jws)
        z.writestr(f"agent-cards/{agent_id}.json", json.dumps(card))
        z.writestr(
            "pack-manifest.json", json.dumps(manifest, sort_keys=True, separators=(",", ":"))
        )
    return bundle


#: Field overrides matching the entry minted by ``_build_pack_bytes``.
_V2_ENTRY_OVERRIDES = {
    "artefact_path": "src/foo.py",
    "agent_id": "agent:t",
    "agent_card_kid": "k1",
    "tool_call_id": "tc-1",
}


def _v2_entry() -> LineageEntry:
    """The entry ``_build_pack_bytes`` signs (the canonical reference)."""
    return LineageEntry(**_sample_entry_dict(**_V2_ENTRY_OVERRIDES))


def _canonical_entry_line() -> bytes:
    """The exact canonical bytes the real writer emits for the test entry."""
    return bernstein_canonicalise(_v2_entry())


def _legacy_entry_line() -> bytes:
    """Pre-fix (non-canonical) bytes: ``json.dumps`` default spaced separators."""
    return json.dumps(asdict(_v2_entry()), sort_keys=True).encode("utf-8")


def test_verify_pack_v2_canonical_verifies(tmp_path):
    """An untampered v2 pack (canonical bytes + trailing newline) verifies."""
    bundle = _build_pack_bytes(tmp_path, log_bytes=_canonical_entry_line() + b"\n")
    result = verify_pack(bundle)
    assert result.ok is True, result.errors


def test_verify_pack_v2_reordered_keys_rejected(tmp_path):
    """Value-preserving tamper: reordered JSON keys must be rejected.

    The fields are unchanged, so re-canonicalising the parsed entry would
    still verify the JWS. Binding to the on-disk bytes is what catches it.
    """
    # Insertion-order dump (NOT sorted) + canonical separators: same values,
    # different byte order from the canonical (sorted-key) form.
    reordered = json.dumps(asdict(_v2_entry()), sort_keys=False, separators=(",", ":")).encode(
        "utf-8"
    )
    assert reordered != _canonical_entry_line()  # guard: genuinely different bytes
    bundle = _build_pack_bytes(tmp_path, log_bytes=reordered + b"\n")
    result = verify_pack(bundle)
    assert result.ok is False
    assert any("canonical" in e.lower() for e in result.errors), result.errors


def test_verify_pack_v2_inserted_whitespace_rejected(tmp_path):
    """Value-preserving tamper: spaced separators (', '/': ') must be rejected."""
    spaced = _canonical_entry_line().replace(b'","', b'", "').replace(b'":', b'": ')
    assert spaced != _canonical_entry_line()
    bundle = _build_pack_bytes(tmp_path, log_bytes=spaced + b"\n")
    result = verify_pack(bundle)
    assert result.ok is False
    assert any("canonical" in e.lower() for e in result.errors), result.errors


def test_verify_pack_v2_missing_trailing_newline_rejected(tmp_path):
    """Value-preserving tamper: stripped trailing newline must be rejected."""
    bundle = _build_pack_bytes(tmp_path, log_bytes=_canonical_entry_line())  # no \n
    result = verify_pack(bundle)
    assert result.ok is False
    assert any("newline" in e.lower() or "trailing" in e.lower() for e in result.errors), (
        result.errors
    )


def test_verify_pack_v2_flipped_terminator_rejected(tmp_path):
    """Value-preserving tamper: flipped \\n -> \\r terminator must be rejected.

    `str.splitlines()` treats `\\r` as a record boundary; splitting strictly
    on `b"\\n"` keeps the stray `\\r` inside the record where the canonical
    check surfaces it.
    """
    bundle = _build_pack_bytes(tmp_path, log_bytes=_canonical_entry_line() + b"\r")
    result = verify_pack(bundle)
    assert result.ok is False


def test_verify_pack_legacy_v1_verifies_under_original_rule(tmp_path):
    """A genuine pre-fix v1 pack (non-canonical bytes, version 1 recorded)
    still verifies under the original re-canonicalise rule.

    Pre-fix packs were written with `json.dumps(..., sort_keys=True)` default
    separators (spaced), so they are NOT canonical on disk. The v1 dispatch
    path must re-canonicalise the parsed entry exactly as before.
    """
    legacy_line = _legacy_entry_line()
    assert legacy_line != _canonical_entry_line()  # genuinely non-canonical
    bundle = _build_pack_bytes(tmp_path, log_bytes=legacy_line + b"\n", pack_format_version=1)
    result = verify_pack(bundle)
    assert result.ok is True, result.errors


def test_verify_pack_no_manifest_defaults_to_legacy_rule(tmp_path):
    """A pack with no manifest (oldest packs) defaults to the v1 rule.

    Mirrors the Merkle seal's legacy-default (#1866): an absent scheme means
    pre-fix, so the original re-canonicalise rule applies and a genuinely
    non-canonical pre-fix log still verifies.
    """
    bundle = _build_pack_bytes(
        tmp_path, log_bytes=_legacy_entry_line() + b"\n", pack_format_version=None
    )
    result = verify_pack(bundle)
    assert result.ok is True, result.errors


def _build_real_pack(tmp_path: Path) -> tuple[Path, LineageEntry]:
    """Build a pack via the real ``build_pack`` and return (zip_path, entry).

    The on-disk source log is written deliberately non-canonically so the
    test proves the writer normalises it to canonical bytes on the way out.
    """
    from datetime import UTC, date, datetime

    from bernstein.core.compliance.pack import build_pack
    from bernstein.core.lineage.identity import AgentCard
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    lineage_dir = tmp_path / "lineage"
    signatures_dir = lineage_dir / "signatures"
    agents_dir = tmp_path / "agents"
    lineage_dir.mkdir()
    signatures_dir.mkdir()
    agents_dir.mkdir()

    _priv_pem, pub_pem = bernstein_generate_keypair()
    agent_id = "agent:worker-1"
    kid = f"{agent_id}-kid"
    card = AgentCard(agent_id=agent_id, kid=kid, public_key_pem=pub_pem)
    (agents_dir / f"{agent_id.replace(':', '_')}.json").write_text(
        json.dumps(asdict(card), sort_keys=True), encoding="utf-8"
    )

    ts_ns = int(datetime(2026, 3, 15, tzinfo=UTC).timestamp() * 1_000_000_000)
    entry = LineageEntry(
        v=1,
        artefact_path="src/in_window.py",
        artefact_kind="file",
        content_hash="sha256:" + hashlib.sha256(b"hello").hexdigest(),
        parent_hashes=[],
        agent_id=agent_id,
        agent_card_kid=kid,
        tool_call_id="tc-1",
        span_id="00f067aa0ba902b7",
        ts_ns=ts_ns,
        operator_hmac="deadbeef",
    )
    # Deliberately non-canonical input (default ``json.dumps`` spacing) so the
    # writer must re-emit it canonically.
    (lineage_dir / "log.jsonl").write_text(
        json.dumps(asdict(entry), sort_keys=True) + "\n", encoding="utf-8"
    )

    op_priv = Ed25519PrivateKey.generate()
    op_key_path = tmp_path / "operator.key"
    op_key_path.write_bytes(
        op_priv.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )

    out_path = tmp_path / "pack.zip"
    build_pack(
        since=date(2026, 1, 1),
        until=date(2026, 5, 13),
        org="Acme",
        lineage_dir=lineage_dir,
        agent_cards_dir=agents_dir,
        output_path=out_path,
        operator_key_path=op_key_path,
    )
    return out_path, entry


def test_real_build_pack_log_is_v2_byte_canonical(tmp_path):
    """A pack from the real ``build_pack`` records v2 and its on-disk log
    bytes pass the v2 byte-binding parse with zero errors.

    This is the writer/verifier lockstep proof for #1871: the bytes the
    production writer emits are exactly the canonical form the offline
    auditor binds to. (The per-entry signature lookup is exercised by the
    fabricated-pack tests above; this test isolates the byte-binding
    contract, which is the subject of #1871.)
    """
    from bernstein.core.compliance.pack import PACK_FORMAT_VERSION

    from bernstein_verify.verify import _pack_format_version, _parse_log_v2

    out_path, _entry = _build_real_pack(tmp_path)

    with zipfile.ZipFile(out_path) as zf:
        assert _pack_format_version(zf) == PACK_FORMAT_VERSION
        log_bytes = zf.read("lineage-log.jsonl")

    entries, errors = _parse_log_v2(log_bytes)
    assert errors == [], errors
    assert len(entries) == 1
    assert entries[0]["artefact_path"] == "src/in_window.py"


def test_real_build_pack_log_byte_tamper_rejected(tmp_path):
    """Value-preserving byte tamper of a real ``build_pack`` log is rejected
    by the v2 byte-binding parse - the end-to-end #1871 guarantee."""
    from bernstein_verify.verify import _parse_log_v2

    out_path, entry = _build_real_pack(tmp_path)

    with zipfile.ZipFile(out_path) as zf:
        canonical_log = zf.read("lineage-log.jsonl")

    # Reorder keys: identical field values, different on-disk bytes.
    reordered = (
        json.dumps(asdict(entry), sort_keys=False, separators=(",", ":")).encode("utf-8") + b"\n"
    )
    assert reordered != canonical_log  # genuinely different bytes

    entries, errors = _parse_log_v2(reordered)
    assert entries == []
    assert any("non-canonical" in e.lower() for e in errors), errors


# ---------- combined: bernstein -> bernstein-verify round-trip ----------


def test_bernstein_signed_entry_verifies_in_verify_cli():
    """The single most important integration assertion in this file:
    an entry signed by bernstein.core.lineage MUST verify with our
    re-implementation."""
    priv, pub = bernstein_generate_keypair()
    entry = LineageEntry(**_sample_entry_dict())
    payload = bernstein_canonicalise(entry)
    jws = bernstein_sign_detached(payload, priv, kid="key-001")

    # Now verify using ONLY bernstein_verify code
    our_payload = jcs_canonicalise(asdict(entry))
    assert our_payload == payload  # canonicalisation must match
    assert verify_jws_detached(our_payload, jws, pub, expected_kid="key-001") is True


# ---------- RFC 6962 copy agrees with the runtime ----------


def test_merkle_copy_and_runtime_agree_on_a_root():
    """The standalone RFC 6962 copy and the runtime hashing produce one root.

    ``bernstein_verify`` must not import ``bernstein.*``, so the leaf/internal
    hashing is a copy of the receipt verifier. This test is the lockstep
    proof that the copy and :mod:`bernstein.core.persistence.merkle` still
    agree, including odd-node promotion on a 5-leaf tree.
    """
    from bernstein.core.persistence.merkle import _combine_internal as runtime_combine
    from bernstein.core.persistence.merkle import _leaf_digest as runtime_leaf
    from bernstein.core.security.audit_receipt import _merkle_root_and_path

    from bernstein_verify.verify import (
        _combine_internal,
        _leaf_digest,
        _merkle_root,
        _root_from_inclusion,
        _verify_manifest_anchor,
    )

    payloads = [f"leaf-{index}".encode() for index in range(5)]
    runtime_leaves = [runtime_leaf(p) for p in payloads]
    copy_leaves = [_leaf_digest(p) for p in payloads]
    assert copy_leaves == runtime_leaves

    runtime_root, path = _merkle_root_and_path(runtime_leaves, 3)
    copy_root = _merkle_root(copy_leaves)
    assert copy_root == runtime_root

    audit_path = [{"hash": sibling, "left": is_left} for sibling, is_left in path]
    assert _root_from_inclusion(copy_leaves[3], audit_path) == runtime_root
    assert _combine_internal(copy_leaves[0], copy_leaves[1]) == runtime_combine(
        runtime_leaves[0], runtime_leaves[1]
    )

    # A transparency-log seal-anchor record is verified through the same
    # manifest-anchor entry the issue names, not the pack output_hash path.
    import base64

    from bernstein.core.security.seal_anchor import transparency_log_leaf
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    head = "ab" * 32
    leaf = transparency_log_leaf(head)
    assert leaf == _leaf_digest(bytes.fromhex(head))
    leaves = [runtime_leaf(f"pad-{i}".encode()) for i in range(4)] + [leaf]
    root, incl = _merkle_root_and_path(leaves, 4)
    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex("11" * 32))
    sth = {"root_hash": root, "tree_size": 5}
    signature = key.sign(json.dumps(sth, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    record = {
        "anchor_kind": "transparency-log",
        "head_sha256": head,
        "leaf_hash": leaf,
        "tree_size": 5,
        "audit_path": [{"hash": h, "left": left} for h, left in incl],
        "signed_tree_head": {
            **sth,
            "signature_b64": base64.b64encode(signature).decode("ascii"),
        },
        "log_public_key": key.public_key().public_bytes_raw().hex(),
    }
    assert _verify_manifest_anchor(record) == []
