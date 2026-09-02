"""Receipt-signing key lifecycle: rotation, revocation, superseded keys (#4211).

A run receipt binds to exactly one Ed25519 key. Operators rotate keys, and
keys get stolen. These tests pin the lifecycle contract:

* rotating a key never invalidates receipts the predecessor signed - the
  auditor still pins one root key and walks the signed succession chain to
  the key that actually signed the receipt in hand;
* a key that was revoked stops carrying trust from its revocation instant
  onwards, and the verdict says which side of that instant the receipt
  falls on rather than collapsing both into "signature failed";
* the chain itself is authenticated and hash-linked, so a stranger cannot
  append a successor and entries cannot be reordered or dropped.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from click.testing import CliRunner
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein.cli.commands.verify_cmd import verify_cmd
from bernstein.core.lineage.spine import LineageSpine
from bernstein.core.replay.journal import EventJournal
from bernstein.core.replay.run_receipt import build_run_receipt, verify_run_receipt
from bernstein.core.security.lineage_kms import FileBasedKMSAdapter
from bernstein.core.security.receipt_key_chain import (
    KeyChainError,
    KeyVerdict,
    append_revocation,
    append_succession,
    new_key_chain,
    serialize_key_chain,
    verify_key_chain,
)

if TYPE_CHECKING:
    from bernstein.core.security.lineage_kms import KMSAdapter

_RUN_ID = "key-lifecycle-fixture"
_HMAC_KEY = b"x" * 32

_ROOT_SEED = b"r" * 32
_SUCCESSOR_SEED = b"s" * 32
_STRANGER_SEED = b"z" * 32

_T1 = "2026-02-01T00:00:00+00:00"
_T2 = "2026-03-01T00:00:00+00:00"


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force a wide terminal so Rich does not soft-wrap the asserted JSON."""
    monkeypatch.setenv("COLUMNS", "400")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_run(sdd_dir: Path, run_id: str = _RUN_ID) -> None:
    journal = EventJournal(run_id=run_id, sdd_dir=sdd_dir)
    journal.record("run_started", run_id=run_id)
    journal.record("task_claimed", task_id="T-1")
    journal.record("run_completed", run_id=run_id)
    spine = LineageSpine(sdd_dir / "lineage", run_id=run_id, hmac_key=_HMAC_KEY)
    spine.record(
        artifact_path="src/app.py",
        content=b"x",
        actor="backend",
        step_id="T-1",
        model="m",
        timestamp=1234,
    )


def _write_key(path: Path, seed: bytes) -> Path:
    key = Ed25519PrivateKey.from_private_bytes(seed)
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return path


def _public_pem(seed: bytes) -> bytes:
    public_key = Ed25519PrivateKey.from_private_bytes(seed).public_key()
    return public_key.public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _adapter(tmp_path: Path, name: str, seed: bytes) -> KMSAdapter:
    return FileBasedKMSAdapter(_write_key(tmp_path / f"{name}.pem", seed), kid=name)


def _receipt_signed_by(tmp_path: Path, adapter: KMSAdapter, run_id: str = _RUN_ID) -> bytes:
    sdd = tmp_path / f"sdd-{run_id}"
    _seed_run(sdd, run_id)
    return build_run_receipt(run_id, sdd, adapter, write=False).receipt_bytes


def _rotated_chain(tmp_path: Path) -> tuple[dict[str, Any], KMSAdapter, KMSAdapter]:
    """Root key rotated to a successor; nothing revoked yet."""
    root = _adapter(tmp_path, "root-kid", _ROOT_SEED)
    successor = _adapter(tmp_path, "successor-kid", _SUCCESSOR_SEED)
    chain = new_key_chain(root.public_key_jwk())
    chain = append_succession(
        chain,
        public_key_jwk=successor.public_key_jwk(),
        issued_at=_T1,
        signer=root,
    )
    return chain, root, successor


# ---------------------------------------------------------------------------
# 1. Rotation keeps prior receipts verifiable against the pinned root
# ---------------------------------------------------------------------------


def test_rotated_key_keeps_prior_receipt_verifiable_against_pinned_root(tmp_path: Path) -> None:
    chain, root, _successor = _rotated_chain(tmp_path)
    receipt = _receipt_signed_by(tmp_path, root)

    result = verify_run_receipt(
        receipt,
        public_key_pem=_public_pem(_ROOT_SEED),
        key_chain_bytes=serialize_key_chain(chain),
    )

    assert result.ok, result.errors
    assert result.key_verdict == KeyVerdict.SUPERSEDED


def test_successor_signed_receipt_verifies_against_the_same_pinned_root(tmp_path: Path) -> None:
    chain, _root, successor = _rotated_chain(tmp_path)
    receipt = _receipt_signed_by(tmp_path, successor)

    result = verify_run_receipt(
        receipt,
        public_key_pem=_public_pem(_ROOT_SEED),
        key_chain_bytes=serialize_key_chain(chain),
    )

    assert result.ok, result.errors
    assert result.key_verdict == KeyVerdict.ACTIVE


# ---------------------------------------------------------------------------
# 2-4. Revocation verdicts
# ---------------------------------------------------------------------------


def _revoked_root_chain(tmp_path: Path) -> tuple[dict[str, Any], KMSAdapter]:
    chain, root, successor = _rotated_chain(tmp_path)
    chain = append_revocation(
        chain,
        kid="root-kid",
        revoked_at=_T2,
        reason="signing host compromised",
        signer=successor,
    )
    return chain, root


def test_receipt_signed_after_revocation_fails_with_a_distinct_verdict(tmp_path: Path) -> None:
    chain, root = _revoked_root_chain(tmp_path)
    receipt = _receipt_signed_by(tmp_path, root)

    result = verify_run_receipt(
        receipt,
        public_key_pem=_public_pem(_ROOT_SEED),
        key_chain_bytes=serialize_key_chain(chain),
        attested_signed_at="2026-04-01T00:00:00+00:00",
    )

    assert not result.ok
    assert result.status == "untrusted_key"
    assert result.key_verdict == KeyVerdict.SIGNED_AFTER_REVOCATION


def test_receipt_signed_before_revocation_keeps_a_separate_verdict(tmp_path: Path) -> None:
    chain, root = _revoked_root_chain(tmp_path)
    receipt = _receipt_signed_by(tmp_path, root)

    result = verify_run_receipt(
        receipt,
        public_key_pem=_public_pem(_ROOT_SEED),
        key_chain_bytes=serialize_key_chain(chain),
        attested_signed_at=_T1,
    )

    assert result.ok, result.errors
    assert result.key_verdict == KeyVerdict.SIGNED_BEFORE_REVOCATION


def test_revoked_key_without_an_attested_signing_time_fails_closed(tmp_path: Path) -> None:
    chain, root = _revoked_root_chain(tmp_path)
    receipt = _receipt_signed_by(tmp_path, root)

    result = verify_run_receipt(
        receipt,
        public_key_pem=_public_pem(_ROOT_SEED),
        key_chain_bytes=serialize_key_chain(chain),
    )

    assert not result.ok
    assert result.status == "untrusted_key"
    assert result.key_verdict == KeyVerdict.REVOKED_SIGNING_TIME_UNKNOWN


# ---------------------------------------------------------------------------
# 5-6. The chain, not the receipt, decides which key is trusted
# ---------------------------------------------------------------------------


def test_key_absent_from_the_chain_is_not_trusted(tmp_path: Path) -> None:
    chain, _root, _successor = _rotated_chain(tmp_path)
    stranger = _adapter(tmp_path, "stranger-kid", _STRANGER_SEED)
    receipt = _receipt_signed_by(tmp_path, stranger)

    result = verify_run_receipt(
        receipt,
        public_key_pem=_public_pem(_ROOT_SEED),
        key_chain_bytes=serialize_key_chain(chain),
    )

    assert not result.ok
    assert result.status == "untrusted_key"
    assert result.key_verdict == KeyVerdict.UNKNOWN_KEY


def test_embedded_key_must_match_the_chain_entry_for_its_kid(tmp_path: Path) -> None:
    """A forger who reuses a trusted ``kid`` with their own key is rejected."""
    chain, _root, _successor = _rotated_chain(tmp_path)
    impostor = _adapter(tmp_path, "root-kid", _STRANGER_SEED)
    receipt = _receipt_signed_by(tmp_path, impostor)

    result = verify_run_receipt(
        receipt,
        public_key_pem=_public_pem(_ROOT_SEED),
        key_chain_bytes=serialize_key_chain(chain),
    )

    assert not result.ok
    assert result.status == "untrusted_key"
    assert result.key_verdict == KeyVerdict.KEY_MISMATCH


# ---------------------------------------------------------------------------
# 7-9. Chain integrity
# ---------------------------------------------------------------------------


def test_succession_signed_by_a_stranger_key_breaks_the_chain(tmp_path: Path) -> None:
    chain, _root, successor = _rotated_chain(tmp_path)
    stranger = _adapter(tmp_path, "stranger-kid", _STRANGER_SEED)
    forged = append_succession(
        chain,
        public_key_jwk=stranger.public_key_jwk(),
        issued_at=_T2,
        signer=stranger,
    )

    with pytest.raises(KeyChainError, match="signature"):
        verify_key_chain(serialize_key_chain(forged), root_public_key_pem=_public_pem(_ROOT_SEED))

    # Sanity: the same append signed by the current head verifies.
    honest = append_succession(
        chain,
        public_key_jwk=stranger.public_key_jwk(),
        issued_at=_T2,
        signer=successor,
    )
    assert verify_key_chain(serialize_key_chain(honest), root_public_key_pem=_public_pem(_ROOT_SEED))


def test_reordering_chain_entries_breaks_the_hash_link(tmp_path: Path) -> None:
    chain, _root = _revoked_root_chain(tmp_path)
    reordered = json.loads(serialize_key_chain(chain))
    reordered["entries"] = list(reversed(reordered["entries"]))

    with pytest.raises(KeyChainError):
        verify_key_chain(
            json.dumps(reordered).encode("utf-8"),
            root_public_key_pem=_public_pem(_ROOT_SEED),
        )


def test_chain_root_must_match_the_pinned_root_key(tmp_path: Path) -> None:
    chain, _root, _successor = _rotated_chain(tmp_path)

    with pytest.raises(KeyChainError, match="root"):
        verify_key_chain(serialize_key_chain(chain), root_public_key_pem=_public_pem(_STRANGER_SEED))


def test_key_chain_supplied_without_a_pinned_root_is_malformed(tmp_path: Path) -> None:
    chain, root, _successor = _rotated_chain(tmp_path)
    receipt = _receipt_signed_by(tmp_path, root)

    result = verify_run_receipt(receipt, key_chain_bytes=serialize_key_chain(chain))

    assert not result.ok
    assert result.status == "malformed"
    assert any("public_key_pem" in err for err in result.errors)


# ---------------------------------------------------------------------------
# 10. Backwards compatibility
# ---------------------------------------------------------------------------


def test_verification_without_a_chain_keeps_its_existing_verdict(tmp_path: Path) -> None:
    root = _adapter(tmp_path, "root-kid", _ROOT_SEED)
    receipt = _receipt_signed_by(tmp_path, root)

    unpinned = verify_run_receipt(receipt)
    pinned = verify_run_receipt(receipt, public_key_pem=_public_pem(_ROOT_SEED))

    assert unpinned.ok and unpinned.key_verdict is None
    assert pinned.ok and pinned.key_verdict is None


# ---------------------------------------------------------------------------
# 11-12. CLI surface
# ---------------------------------------------------------------------------


def _cli_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    chain, root = _revoked_root_chain(tmp_path)
    receipt_path = tmp_path / "run-receipt.json"
    receipt_path.write_bytes(_receipt_signed_by(tmp_path, root))
    chain_path = tmp_path / "key-chain.json"
    chain_path.write_bytes(serialize_key_chain(chain))
    root_pub = tmp_path / "root.pub.pem"
    root_pub.write_bytes(_public_pem(_ROOT_SEED))
    return receipt_path, chain_path, root_pub


def test_verify_receipt_cli_exits_four_when_the_signing_key_was_revoked(tmp_path: Path) -> None:
    receipt_path, chain_path, root_pub = _cli_fixture(tmp_path)

    result = CliRunner().invoke(
        verify_cmd,
        [
            "receipt",
            str(receipt_path),
            "--public-key",
            str(root_pub),
            "--key-chain",
            str(chain_path),
            "--signed-at",
            "2026-04-01T00:00:00+00:00",
            "--json",
        ],
    )

    assert result.exit_code == 4, result.output
    payload = json.loads(result.output)
    assert payload["key_verdict"] == "signed-after-revocation"
    assert payload["exit_code"] == 4


def test_verify_receipt_cli_accepts_a_superseded_key(tmp_path: Path) -> None:
    chain, root, _successor = _rotated_chain(tmp_path)
    receipt_path = tmp_path / "run-receipt.json"
    receipt_path.write_bytes(_receipt_signed_by(tmp_path, root))
    chain_path = tmp_path / "key-chain.json"
    chain_path.write_bytes(serialize_key_chain(chain))
    root_pub = tmp_path / "root.pub.pem"
    root_pub.write_bytes(_public_pem(_ROOT_SEED))

    result = CliRunner().invoke(
        verify_cmd,
        [
            "receipt",
            str(receipt_path),
            "--public-key",
            str(root_pub),
            "--key-chain",
            str(chain_path),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["key_verdict"] == "superseded"


# ---------------------------------------------------------------------------
# 13. Determinism of the chain document
# ---------------------------------------------------------------------------


def test_appending_to_a_chain_leaves_earlier_entries_byte_identical(tmp_path: Path) -> None:
    chain, _root, successor = _rotated_chain(tmp_path)
    before = json.loads(serialize_key_chain(chain))["entries"]
    extended = append_revocation(
        chain,
        kid="root-kid",
        revoked_at=_T2,
        reason="rotation drill",
        signer=successor,
    )
    after = json.loads(serialize_key_chain(extended))["entries"]

    assert after[: len(before)] == before
    assert base64.b64decode(after[-1]["signature_b64"], validate=True)
