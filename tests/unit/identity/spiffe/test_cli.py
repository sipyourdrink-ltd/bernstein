"""CLI surface for SPIFFE workload identity (issue #2363).

``bernstein spiffe id`` derives the deterministic id offline; ``bernstein spiffe
verify-binding`` re-derives it from the install key and, with ``--audit-dir``,
proves the card-to-SVID binding against its chained receipt.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from bernstein.cli.commands.spiffe_cmd import spiffe_group
from bernstein.core.identity.spiffe import (
    bind_svid_to_card,
    derive_spiffe_id_from_key,
    svid_reference_from_x509,
)
from bernstein.core.identity.spiffe.svid import X509Svid
from bernstein.core.security.agent_identity import issue_identity_card
from bernstein.core.security.audit_chain import AuditChainStore


def test_id_command_deterministic(tmp_path: Path, install_keypair) -> None:
    _priv, pub = install_keypair
    key_file = tmp_path / "pub.pem"
    key_file.write_bytes(pub)
    runner = CliRunner()
    result = runner.invoke(
        spiffe_group,
        ["id", "--install-key", str(key_file), "--agent", "backend-1", "--trust-domain", "ex.org"],
    )
    assert result.exit_code == 0, result.output
    expected = derive_spiffe_id_from_key(trust_domain="ex.org", install_public_key_pem=pub, agent_id="backend-1")
    assert result.output.strip() == expected


def _write_binding(tmp_path: Path, install_keypair, svid_leaf_factory) -> tuple[Path, Path, Path]:
    _priv, pub = install_keypair
    key_file = tmp_path / "pub.pem"
    key_file.write_bytes(pub)
    sid = derive_spiffe_id_from_key(trust_domain="ex.org", install_public_key_pem=pub, agent_id="backend-1")
    cert_pem, key_pem = svid_leaf_factory(sid)
    svid = X509Svid(
        spiffe_id=sid, cert_chain_pem=cert_pem, private_key_pem=key_pem, bundle_pem=cert_pem, expires_at=0.0
    )
    ref = svid_reference_from_x509(svid)
    card = issue_identity_card("backend-1", "backend", "claude", "opus")
    audit_dir = tmp_path / "audit"
    chain = AuditChainStore(audit_dir, key=b"k" * 32)
    _updated, binding, _event = bind_svid_to_card(
        card=card, svid_reference=ref, install_public_key_pem=pub, trust_domain="ex.org", chain=chain
    )
    binding_file = tmp_path / "binding.json"
    binding_file.write_text(json.dumps(binding.to_dict()), encoding="utf-8")
    return binding_file, key_file, audit_dir


def test_verify_binding_ok(tmp_path: Path, install_keypair, svid_leaf_factory) -> None:
    binding_file, key_file, _audit = _write_binding(tmp_path, install_keypair, svid_leaf_factory)
    runner = CliRunner()
    result = runner.invoke(
        spiffe_group,
        ["verify-binding", str(binding_file), "--install-key", str(key_file), "--trust-domain", "ex.org"],
    )
    assert result.exit_code == 0, result.output
    assert "valid" in result.output


def test_verify_binding_chain_anchored(tmp_path: Path, install_keypair, svid_leaf_factory) -> None:
    binding_file, key_file, audit_dir = _write_binding(tmp_path, install_keypair, svid_leaf_factory)
    runner = CliRunner()
    result = runner.invoke(
        spiffe_group,
        [
            "verify-binding",
            str(binding_file),
            "--install-key",
            str(key_file),
            "--trust-domain",
            "ex.org",
            "--audit-dir",
            str(audit_dir),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "chain-anchored" in result.output


def test_verify_binding_wrong_trust_domain_fails(tmp_path: Path, install_keypair, svid_leaf_factory) -> None:
    binding_file, key_file, _audit = _write_binding(tmp_path, install_keypair, svid_leaf_factory)
    runner = CliRunner()
    result = runner.invoke(
        spiffe_group,
        ["verify-binding", str(binding_file), "--install-key", str(key_file), "--trust-domain", "evil.org"],
    )
    assert result.exit_code == 1
    assert "invalid" in result.output
