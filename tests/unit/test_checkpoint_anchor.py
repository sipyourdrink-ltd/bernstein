"""External RFC 3161 anchors over audit checkpoints (#5036).

``chain_checkpoint`` makes an audit-history shrink sticky only against
material we hold ourselves: an actor with write access to both the chain
segments and the checkpoints file can truncate both to a mutually
consistent earlier state and every local verification still passes. These
tests pin the behaviour of the anchor that closes that residual - a
timestamp token a third party signed over a checkpoint's canonical bytes,
stored beside the checkpoint and never inside it.

The tests mint their own TimeStampTokens from a throwaway CA so the
messageImprint can cover a checkpoint produced in the test. No network
call is made, here or in the product code.
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from click.testing import CliRunner
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from bernstein.cli.commands.audit_cmd import audit_group
from bernstein.core.persistence.chain_checkpoint import (
    checkpoints_path,
    count_entries,
    load_checkpoints,
    record_checkpoint,
)
from bernstein.core.persistence.checkpoint_anchor import (
    AnchorFileError,
    anchoring_state,
    anchors_path,
    check_anchor_contradictions,
    checkpoint_digest,
    load_anchors,
    record_anchor,
)
from bernstein.core.persistence.merkle import compute_seal
from bernstein.core.security.audit import AuditLog

if TYPE_CHECKING:
    from pathlib import Path

_KEY = b"anchor-substrate-test-key-0123456"


# ---------------------------------------------------------------------------
# A throwaway TSA: mints real RFC 3161 tokens over arbitrary digests.
# ---------------------------------------------------------------------------


class _TestTSA:
    """Self-contained TSA: a root CA plus a time-stamping leaf.

    Real ASN.1, real RSA signatures, real trust chain - the product
    verifier is exercised end-to-end. Only the trust anchor is local.
    """

    def __init__(self) -> None:
        self.ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = datetime.now(UTC)
        ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Bernstein Test TSA Root")])
        self.ca_cert = (
            x509.CertificateBuilder()
            .subject_name(ca_name)
            .issuer_name(ca_name)
            .public_key(self.ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(self.ca_key.public_key()), critical=False)
            .sign(self.ca_key, hashes.SHA256())
        )
        self.leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.leaf_cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Bernstein Test TSA")]))
            .issuer_name(self.ca_cert.subject)
            .public_key(self.leaf_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.TIME_STAMPING]), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(self.leaf_key.public_key()), critical=False)
            .sign(self.ca_key, hashes.SHA256())
        )

    def token(self, digest: bytes) -> bytes:
        """Return a DER TimeStampToken whose messageImprint covers *digest*."""
        from asn1crypto import cms, tsp
        from asn1crypto import x509 as asn1_x509

        tst = tsp.TSTInfo(
            {
                "version": "v1",
                "policy": "1.3.6.1.4.1.99999.1",
                "message_imprint": {
                    "hash_algorithm": {"algorithm": "sha256"},
                    "hashed_message": digest,
                },
                "serial_number": 1,
                "gen_time": datetime.now(UTC),
            },
        )
        tst_bytes = tst.dump()
        signed_attrs = cms.CMSAttributes(
            [
                cms.CMSAttribute({"type": "content_type", "values": ["tst_info"]}),
                cms.CMSAttribute({"type": "message_digest", "values": [hashlib.sha256(tst_bytes).digest()]}),
            ],
        )
        signature = self.leaf_key.sign(signed_attrs.dump(), padding.PKCS1v15(), hashes.SHA256())
        signer_info = cms.SignerInfo(
            {
                "version": "v1",
                "sid": cms.SignerIdentifier(
                    {
                        "issuer_and_serial_number": cms.IssuerAndSerialNumber(
                            {
                                "issuer": asn1_x509.Name.load(self.leaf_cert.issuer.public_bytes()),
                                "serial_number": self.leaf_cert.serial_number,
                            },
                        ),
                    },
                ),
                "digest_algorithm": {"algorithm": "sha256"},
                "signed_attrs": signed_attrs,
                "signature_algorithm": {"algorithm": "rsassa_pkcs1v15"},
                "signature": signature,
            },
        )
        signed_data = cms.SignedData(
            {
                "version": "v3",
                "digest_algorithms": [{"algorithm": "sha256"}],
                "encap_content_info": {
                    "content_type": "tst_info",
                    "content": cms.ParsableOctetString(tst_bytes),
                },
                "certificates": [
                    asn1_x509.Certificate.load(self.leaf_cert.public_bytes(serialization.Encoding.DER)),
                    asn1_x509.Certificate.load(self.ca_cert.public_bytes(serialization.Encoding.DER)),
                ],
                "signer_infos": [signer_info],
            },
        )
        return cms.ContentInfo({"content_type": "signed_data", "content": signed_data}).dump()

    def trust_bundle(self, path: Path) -> Path:
        path.write_bytes(self.ca_cert.public_bytes(serialization.Encoding.PEM))
        return path


@pytest.fixture(scope="module")
def tsa() -> _TestTSA:
    return _TestTSA()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed(tmp_path: Path, count: int = 6) -> Path:
    audit_dir = tmp_path / ".sdd" / "audit"
    log = AuditLog(audit_dir, key=_KEY)
    for i in range(count):
        log.log("test.event", "tester", "task", f"t-{i}", {"i": i})
    return audit_dir


def _seal_and_pin(audit_dir: Path) -> dict[str, Any]:
    _tree, seal = compute_seal(audit_dir, key=_KEY)
    return record_checkpoint(audit_dir, seal, key=_KEY)


def _truncate_history(audit_dir: Path, keep: int) -> None:
    """Drop all but the first *keep* records from the live segment.

    The chain stays HMAC-intact (a prefix of a chain is a valid chain), so
    only a pin outside the truncated material can notice.
    """
    segment = next(iter(sorted(audit_dir.glob("*.jsonl"))))
    lines = segment.read_bytes().split(b"\n")
    body = [line for line in lines if line.strip()]
    segment.write_bytes(b"".join(line + b"\n" for line in body[:keep]))


def _cli_seal(audit_dir: Path) -> dict[str, Any]:
    """Seal through the CLI (writes the seal file) and return the new checkpoint."""
    result = CliRunner().invoke(audit_group, ["seal"])
    assert result.exit_code == 0, result.output
    checkpoint = load_checkpoints(audit_dir, _KEY).last
    assert checkpoint is not None
    return checkpoint


@pytest.fixture()
def cli_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project root with a seeded audit chain and the key on disk."""
    key_file = tmp_path / "audit.key"
    key_file.write_bytes(_KEY)
    key_file.chmod(0o600)
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(key_file))
    monkeypatch.setenv("COLUMNS", "200")
    _seed(tmp_path)
    monkeypatch.chdir(tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# 1. Anchoring a checkpoint
# ---------------------------------------------------------------------------


def test_checkpoint_can_be_anchored_with_an_rfc3161_token(tmp_path: Path, tsa: _TestTSA) -> None:
    """A token over the checkpoint's canonical bytes is recorded beside it."""
    audit_dir = _seed(tmp_path)
    checkpoint = _seal_and_pin(audit_dir)

    token = tsa.token(checkpoint_digest(checkpoint))
    anchor = record_anchor(audit_dir, checkpoint, token, tsa_url="https://tsa.example/tsr")

    assert anchor.entry_count == checkpoint["entry_count"]
    assert anchor.checkpoint_root == checkpoint["root_hash"]
    assert anchor.payload_sha256 == checkpoint_digest(checkpoint).hex()
    assert anchor.tsa_url == "https://tsa.example/tsr"

    stored = load_anchors(audit_dir)
    assert [a.payload_sha256 for a in stored] == [anchor.payload_sha256]
    assert anchors_path(audit_dir).exists()


def test_anchor_refuses_a_token_that_covers_other_bytes(tmp_path: Path, tsa: _TestTSA) -> None:
    """A token for some other payload cannot be filed against this checkpoint."""
    audit_dir = _seed(tmp_path)
    checkpoint = _seal_and_pin(audit_dir)

    with pytest.raises(ValueError, match="messageImprint"):
        record_anchor(audit_dir, checkpoint, tsa.token(hashlib.sha256(b"other").digest()))
    assert not anchors_path(audit_dir).exists()


# ---------------------------------------------------------------------------
# 2. Determinism
# ---------------------------------------------------------------------------


def test_anchored_checkpoint_payload_is_still_byte_identical_without_the_token(
    tmp_path: Path,
    tsa: _TestTSA,
) -> None:
    """The token lives beside the checkpoint, never inside its canonical bytes."""
    audit_dir = _seed(tmp_path)
    checkpoint = _seal_and_pin(audit_dir)
    before = checkpoints_path(audit_dir).read_bytes()
    entries_before = count_entries(audit_dir)

    record_anchor(audit_dir, checkpoint, tsa.token(checkpoint_digest(checkpoint)))

    assert checkpoints_path(audit_dir).read_bytes() == before
    assert load_checkpoints(audit_dir, _KEY).last == checkpoint
    assert checkpoint_digest(checkpoint) == hashlib.sha256(_payload_bytes(before)).digest()
    # The anchors file must not be mistaken for a chain segment.
    assert count_entries(audit_dir) == entries_before

    # A later checkpoint is unaffected by the anchor beside its predecessor.
    AuditLog(audit_dir, key=_KEY).log("test.event", "tester", "task", "extra", {})
    second = _seal_and_pin(audit_dir)
    assert set(second) == set(checkpoint)
    assert not any(key.startswith("anchor") for key in second)


def _payload_bytes(raw: bytes) -> bytes:
    """Canonical bytes of the last checkpoint payload in a checkpoints file."""
    doc = json.loads(raw.splitlines()[-1])
    return json.dumps(doc["payload"], sort_keys=True, separators=(",", ":")).encode()


# ---------------------------------------------------------------------------
# 3. Contradiction - the load-bearing property
# ---------------------------------------------------------------------------


def test_verification_fails_when_history_is_shorter_than_the_newest_anchor(
    cli_workspace: Path,
    tsa: _TestTSA,
) -> None:
    """A rollback of chain AND checkpoints still contradicts the outside anchor."""
    audit_dir = cli_workspace / ".sdd" / "audit"
    checkpoint = _cli_seal(audit_dir)
    record_anchor(audit_dir, checkpoint, tsa.token(checkpoint_digest(checkpoint)))

    runner = CliRunner()
    assert runner.invoke(audit_group, ["verify"]).exit_code == 0

    # The full local rollback: history shrinks and the pin that would have
    # noticed is deleted with it.
    _truncate_history(audit_dir, keep=2)
    checkpoints_path(audit_dir).unlink()

    result = runner.invoke(audit_group, ["verify"])
    assert result.exit_code == 1, result.output
    assert "External Anchors" in result.output


def test_verification_names_the_contradicted_anchor_in_its_error(
    cli_workspace: Path,
    tsa: _TestTSA,
) -> None:
    """The failure names the anchor: its entry count, root, and TSA."""
    audit_dir = cli_workspace / ".sdd" / "audit"
    checkpoint = _cli_seal(audit_dir)
    record_anchor(
        audit_dir,
        checkpoint,
        tsa.token(checkpoint_digest(checkpoint)),
        tsa_url="https://tsa.example/tsr",
    )
    _truncate_history(audit_dir, keep=2)
    checkpoints_path(audit_dir).unlink()

    result = CliRunner().invoke(audit_group, ["verify"])
    assert result.exit_code == 1, result.output
    assert str(checkpoint["entry_count"]) in result.output
    assert str(checkpoint["root_hash"])[:16] in result.output
    assert "https://tsa.example/tsr" in result.output


def test_anchor_contradiction_is_not_clearable_by_acknowledgement(
    cli_workspace: Path,
    tsa: _TestTSA,
) -> None:
    """An external statement cannot be waived by a local acknowledgement."""
    from bernstein.core.persistence.chain_checkpoint import CheckpointConflict

    audit_dir = cli_workspace / ".sdd" / "audit"
    checkpoint = _cli_seal(audit_dir)
    record_anchor(audit_dir, checkpoint, tsa.token(checkpoint_digest(checkpoint)))
    _truncate_history(audit_dir, keep=2)

    conflicts = check_anchor_contradictions(audit_dir, load_anchors(audit_dir))
    assert conflicts
    assert all(c.kind not in CheckpointConflict.ACKABLE_KINDS for c in conflicts)


# ---------------------------------------------------------------------------
# 5 + 6. Doctor and the air-gapped install
# ---------------------------------------------------------------------------


def test_doctor_reports_never_anchored_on_a_fresh_install(cli_workspace: Path) -> None:
    """An unanchored install says so rather than implying a stronger seal."""
    from bernstein.cli.commands.doctor_cmd import check_audit_anchoring
    from bernstein.cli.commands.status_cmd import _doctor_check_audit_anchoring

    row = check_audit_anchoring(cli_workspace)
    assert row["name"] == "Audit anchoring"
    assert row["status"] == "WARN"
    assert "never" in row["detail"].lower()
    assert "bernstein audit anchor" in row["fix"]

    # The row reaches the report `bernstein doctor` actually prints.
    rendered: list[dict[str, Any]] = []
    _doctor_check_audit_anchoring(rendered, cli_workspace)
    assert [c["name"] for c in rendered] == ["Audit anchoring"]
    assert rendered[0]["ok"] is True
    assert rendered[0]["detail"].startswith("WARNING: ")


def test_airgapped_install_runs_without_a_tsa_and_says_it_is_unanchored(cli_workspace: Path) -> None:
    """No TSA, no anchors: verify still passes and names the missing anchor."""
    audit_dir = cli_workspace / ".sdd" / "audit"
    _cli_seal(audit_dir)

    result = CliRunner().invoke(audit_group, ["verify"])
    assert result.exit_code == 0, result.output
    assert "not externally anchored" in result.output.lower()

    state = anchoring_state(audit_dir)
    assert state.anchored is False
    assert state.newest_gen_time is None


# ---------------------------------------------------------------------------
# 7. A token that cannot be checked must not pass
# ---------------------------------------------------------------------------


def test_missing_or_invalid_token_does_not_silently_pass_verification(
    cli_workspace: Path,
    tsa: _TestTSA,
) -> None:
    """A corrupt, absent, or untrusted token fails the pillar loudly."""
    audit_dir = cli_workspace / ".sdd" / "audit"
    checkpoint = _cli_seal(audit_dir)
    record_anchor(audit_dir, checkpoint, tsa.token(checkpoint_digest(checkpoint)))

    runner = CliRunner()
    assert runner.invoke(audit_group, ["verify"]).exit_code == 0

    path = anchors_path(audit_dir)
    good = json.loads(path.read_text(encoding="utf-8").splitlines()[0])

    # (a) The token bytes are replaced with something that is not a token.
    corrupt = {**good, "token_b64": base64.b64encode(b"not a timestamp token").decode("ascii")}
    path.write_text(json.dumps(corrupt, sort_keys=True) + "\n", encoding="utf-8")
    assert runner.invoke(audit_group, ["verify"]).exit_code == 1

    # (b) The token is gone entirely; the claim about history remains.
    missing = {k: v for k, v in good.items() if k != "token_b64"}
    path.write_text(json.dumps(missing, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(AnchorFileError):
        load_anchors(audit_dir)
    assert runner.invoke(audit_group, ["verify"]).exit_code == 1


def test_trust_bundle_rejects_a_token_from_an_unknown_tsa(
    cli_workspace: Path,
    tsa: _TestTSA,
    tmp_path: Path,
) -> None:
    """With operator trust anchors supplied, a foreign TSA fails the chain walk."""
    audit_dir = cli_workspace / ".sdd" / "audit"
    checkpoint = _cli_seal(audit_dir)
    record_anchor(audit_dir, checkpoint, tsa.token(checkpoint_digest(checkpoint)))

    runner = CliRunner()
    trusted = tsa.trust_bundle(tmp_path / "trusted.pem")
    ok = runner.invoke(audit_group, ["verify", "--rfc3161-trusted-tsa-bundle", str(trusted)])
    assert ok.exit_code == 0, ok.output

    other = _TestTSA().trust_bundle(tmp_path / "other.pem")
    bad = runner.invoke(audit_group, ["verify", "--rfc3161-trusted-tsa-bundle", str(other)])
    assert bad.exit_code == 1, bad.output


def test_anchor_command_round_trips_a_der_tsa_response(
    cli_workspace: Path,
    tsa: _TestTSA,
    tmp_path: Path,
) -> None:
    """The operator flow: print the digest, hand back the TSA response, verify."""
    audit_dir = cli_workspace / ".sdd" / "audit"
    checkpoint = _cli_seal(audit_dir)
    runner = CliRunner()

    request = runner.invoke(audit_group, ["anchor", "--print-request"])
    assert request.exit_code == 0, request.output
    digest = checkpoint_digest(checkpoint).hex()
    assert digest in request.output.replace("\n", "")

    # openssl hands the operator raw DER, not the base64 form 'export' takes.
    token_file = tmp_path / "resp.tsr"
    token_file.write_bytes(tsa.token(checkpoint_digest(checkpoint)))
    recorded = runner.invoke(
        audit_group,
        ["anchor", "--rfc3161-token", str(token_file), "--rfc3161-tsa-url", "https://tsa.example/tsr"],
    )
    assert recorded.exit_code == 0, recorded.output

    stored = load_anchors(audit_dir)
    assert [a.entry_count for a in stored] == [checkpoint["entry_count"]]
    assert runner.invoke(audit_group, ["verify"]).exit_code == 0


def test_anchor_command_refuses_a_token_for_another_checkpoint(
    cli_workspace: Path,
    tsa: _TestTSA,
    tmp_path: Path,
) -> None:
    """Nothing is filed when the TSA never saw this checkpoint."""
    audit_dir = cli_workspace / ".sdd" / "audit"
    _cli_seal(audit_dir)
    token_file = tmp_path / "resp.tsr"
    token_file.write_bytes(tsa.token(hashlib.sha256(b"some other statement").digest()))

    result = CliRunner().invoke(audit_group, ["anchor", "--rfc3161-token", str(token_file)])
    assert result.exit_code != 0
    assert not anchors_path(audit_dir).exists()
