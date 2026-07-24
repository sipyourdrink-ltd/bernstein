"""``bernstein delegation verify-token`` CLI (issue #2611, AC6).

The command verifies a signed capability-token chain fully offline, prints
per-hop pass/fail, and exits non-zero on any failing hop.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from click.testing import CliRunner

from bernstein.cli.commands.delegation_cmd import delegation_group
from bernstein.core.security import capability_tokens as ct
from bernstein.core.security.agent_card_signer import generate_ed25519_keypair

_NOW = 1_800_000_000.0


def _caveats(perms: set[str], depth: int) -> ct.Caveats:
    return ct.Caveats(permissions=frozenset(perms), remaining_depth=depth, not_after=_NOW + 3600)


def _write_chain(tmp_path: Path) -> tuple[Path, Path]:
    p_priv, p_pub = generate_ed25519_keypair()
    o_priv, o_pub = generate_ed25519_keypair()
    _s_priv, s_pub = generate_ed25519_keypair()

    root = ct.mint_root(
        issuer_identity_id="principal",
        issuer_private_key=p_priv,
        subject_identity_id="orchestrator",
        subject_pubkey=o_pub,
        caveats=_caveats({"files:read", "files:write"}, depth=3),
    )
    hop1 = ct.attenuate(
        root,
        issuer_private_key=o_priv,
        subject_identity_id="sub-agent",
        subject_pubkey=s_pub,
        caveats=_caveats({"files:read"}, depth=2),
    )
    chain = ct.CapabilityChain(tokens=(root, hop1))

    token_file = tmp_path / "chain.json"
    token_file.write_text(chain.to_json(), encoding="utf-8")
    anchor_file = tmp_path / "principal.pem"
    anchor_file.write_bytes(p_pub)
    return token_file, anchor_file


def test_verify_token_valid_exits_zero(tmp_path: Path) -> None:
    token_file, anchor_file = _write_chain(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        delegation_group,
        ["verify-token", str(token_file), "--trust-anchor", str(anchor_file)],
    )
    assert result.exit_code == 0, result.output
    assert "hop 0: principal -> orchestrator  PASS" in result.output
    assert "hop 1: orchestrator -> sub-agent  PASS" in result.output
    assert "capability chain verified" in result.output


def test_verify_token_tampered_exits_nonzero(tmp_path: Path) -> None:
    token_file, anchor_file = _write_chain(tmp_path)
    chain = ct.CapabilityChain.from_json(token_file.read_text(encoding="utf-8"))
    tokens = list(chain.tokens)
    # Corrupt hop 1's signature (first significant base64url char).
    header, _payload, sig = tokens[1].jws.split(".")
    flipped = "A" if sig[0] != "A" else "B"
    tokens[1] = dataclasses.replace(tokens[1], jws=f"{header}..{flipped}{sig[1:]}")
    token_file.write_text(ct.CapabilityChain(tokens=tuple(tokens)).to_json(), encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        delegation_group,
        ["verify-token", str(token_file), "--trust-anchor", str(anchor_file)],
    )
    assert result.exit_code == 1, result.output
    assert "hop 1:" in result.output and "FAIL" in result.output
    assert "capability chain verification failed" in result.output


def test_verify_token_untrusted_root_exits_nonzero(tmp_path: Path) -> None:
    token_file, _anchor_file = _write_chain(tmp_path)
    _other_priv, other_pub = generate_ed25519_keypair()
    other_file = tmp_path / "other.pem"
    other_file.write_bytes(other_pub)

    runner = CliRunner()
    result = runner.invoke(
        delegation_group,
        ["verify-token", str(token_file), "--trust-anchor", str(other_file)],
    )
    assert result.exit_code == 1, result.output
    assert "hop 0:" in result.output and "FAIL" in result.output


def test_verify_token_json_output(tmp_path: Path) -> None:
    token_file, anchor_file = _write_chain(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        delegation_group,
        ["verify-token", str(token_file), "--trust-anchor", str(anchor_file), "--json"],
    )
    assert result.exit_code == 0, result.output
    import json

    payload = json.loads(result.output)
    assert payload["valid"] is True
    assert payload["principal_path"] == ["principal", "orchestrator", "sub-agent"]
    assert all(hop["ok"] for hop in payload["hops"])
