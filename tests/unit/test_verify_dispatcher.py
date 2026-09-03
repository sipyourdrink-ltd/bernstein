"""Tests for the ``bernstein verify <artefact>`` kind-detecting dispatcher (#5103).

Slice 1 of #5103: the registry-backed dispatcher itself plus two wired kinds
(``bom``, ``receipt-bundle``) -- enough to prove the shape, not all 55
groups. See the PR body for what is explicitly out of scope in this slice.
"""

from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path

import pytest
from click.testing import CliRunner
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein.cli.commands.bom_cmd import bom_group
from bernstein.cli.commands.receipt_cmd import receipt_group
from bernstein.cli.commands.verify_cmd import verify_cmd
from bernstein.cli.commands.verify_kinds import register_default_verifiers
from bernstein.core.compliance.ai_bom import encode_bom, generate_bom
from bernstein.core.verify_dispatch import (
    EXIT_UNKNOWN_KIND,
    DuplicateVerifierKindError,
    VerifierSpec,
    VerifyOutcome,
    detect_kind,
    dispatch_verify,
    register_verifier,
)


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch: pytest.MonkeyPatch):
    """Every test starts from an empty registry, unregistered defaults.

    The registry is process-global; without this, test order would decide
    whether ``register_verifier`` raises on a kind another test already
    registered.
    """
    import bernstein.cli.commands.verify_kinds as verify_kinds_module
    import bernstein.core.verify_dispatch as verify_dispatch_module

    monkeypatch.setattr(verify_dispatch_module, "_REGISTRY", {})
    monkeypatch.setattr(verify_kinds_module, "_registered", False)
    yield


def _bom_snapshot() -> dict:
    return {
        "run_id": "20260902-verify-dispatch",
        "started_at": "2026-09-02T00:00:00Z",
        "finished_at": "2026-09-02T00:01:00Z",
        "lineage_root_hash": "sha256:" + "a" * 64,
        "bernstein_version": "3.0.0",
        "models": [],
        "prompts": [],
        "adapters": [],
        "tools": [],
        "data_sources": [],
    }


def _write_bom(tmp_path: Path) -> Path:
    bom = generate_bom(_bom_snapshot())
    out = tmp_path / "sbom.json"
    out.write_bytes(encode_bom(bom, fmt="json"))
    return out


def _write_receipt_bundle(tmp_path: Path) -> Path:
    key = Ed25519PrivateKey.from_private_bytes(bytes([9]) * 32)
    key_path = tmp_path / "worker.pem"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(
        json.dumps(
            {
                "task": {"repo": "sipyourdrink-ltd/bernstein", "commit_sha": "abc123def456", "issue_number": 5103},
                "patch": "diff --git a/x b/x\n+hi\n",
                "gates": [{"command": "pytest -q", "exit_code": 0, "log": "ok\n"}],
                "manifest_sha256": "0" * 64,
                "adapter_id": "adapter.default.v3",
                "model_id": "claude-x",
                "sandbox_profile": "restricted-net-off",
                "selection_receipt": "sel-1",
                "created_at": "2026-09-02T00:00:00Z",
                "chain": {"anchor": "genesis", "length": 1},
            }
        ),
        encoding="utf-8",
    )
    bundle_path = tmp_path / "bundle.json"
    result = CliRunner().invoke(
        receipt_group,
        ["create", str(spec_path), "--signing-key", str(key_path), "-o", str(bundle_path)],
    )
    assert result.exit_code == 0, result.output
    return bundle_path


# ---------------------------------------------------------------------------
# test_registered_kinds_route_through_the_dispatcher
# ---------------------------------------------------------------------------
# Load-bearing: fails on main today -- there is no dispatcher module, and no
# `bernstein verify <artefact>` that inspects an artefact to pick a verifier.


def test_bom_artefact_routes_to_the_bom_verifier(tmp_path: Path) -> None:
    register_default_verifiers()
    outcome = dispatch_verify(_write_bom(tmp_path))
    assert outcome.kind == "bom"
    assert outcome.ok is True
    assert outcome.exit_code == 0


def test_receipt_bundle_artefact_routes_to_the_receipt_bundle_verifier(tmp_path: Path) -> None:
    register_default_verifiers()
    outcome = dispatch_verify(_write_receipt_bundle(tmp_path))
    assert outcome.kind == "receipt-bundle"
    assert outcome.ok is True
    assert outcome.exit_code == 0


# ---------------------------------------------------------------------------
# test_old_and_new_verify_spellings_agree
# ---------------------------------------------------------------------------


def test_bom_group_and_dispatcher_agree_on_success(tmp_path: Path) -> None:
    path = _write_bom(tmp_path)
    old = CliRunner().invoke(bom_group, ["verify", str(path)])
    new = CliRunner().invoke(verify_cmd, [str(path)])
    assert old.exit_code == 0
    assert new.exit_code == 0


def test_receipt_group_and_dispatcher_agree_on_success(tmp_path: Path) -> None:
    path = _write_receipt_bundle(tmp_path)
    old = CliRunner().invoke(receipt_group, ["verify", str(path)])
    new = CliRunner().invoke(verify_cmd, [str(path)])
    assert old.exit_code == 0
    assert new.exit_code == 0


def test_a_tampered_bom_fails_identically_both_ways(tmp_path: Path) -> None:
    path = _write_bom(tmp_path)
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["schema_version"] = "9.9.9"
    path.write_text(json.dumps(doc), encoding="utf-8")

    old = CliRunner().invoke(bom_group, ["verify", str(path)])
    new = CliRunner().invoke(verify_cmd, [str(path)])
    assert old.exit_code == 1
    assert new.exit_code == 1


def test_a_tampered_receipt_bundle_fails_identically_both_ways(tmp_path: Path) -> None:
    path = _write_receipt_bundle(tmp_path)
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["signatures"][0]["sig"] = doc["signatures"][0]["sig"][:-4] + "AAAA"
    path.write_text(json.dumps(doc), encoding="utf-8")

    old = CliRunner().invoke(receipt_group, ["verify", str(path)])
    new = CliRunner().invoke(verify_cmd, [str(path)])
    assert old.exit_code == 1
    assert new.exit_code == 1


# ---------------------------------------------------------------------------
# test_unrecognised_artefact_kind_reports_clearly
# ---------------------------------------------------------------------------


def test_unrecognised_artefact_kind_reports_clearly(tmp_path: Path) -> None:
    register_default_verifiers()
    mystery = tmp_path / "mystery.json"
    mystery.write_text(json.dumps({"hello": "world"}), encoding="utf-8")

    outcome = dispatch_verify(mystery)

    assert outcome.kind == "unknown"
    assert outcome.ok is False
    assert outcome.exit_code == EXIT_UNKNOWN_KIND
    assert str(mystery) in outcome.message


def test_unrecognised_kind_via_cli_exits_distinctly_from_a_failed_verification(tmp_path: Path) -> None:
    mystery = tmp_path / "mystery.json"
    mystery.write_text(json.dumps({"hello": "world"}), encoding="utf-8")

    result = CliRunner().invoke(verify_cmd, [str(mystery)])

    assert result.exit_code == EXIT_UNKNOWN_KIND


# ---------------------------------------------------------------------------
# test_json_shape_identical_across_kinds
# ---------------------------------------------------------------------------
# The property the dispatcher exists to guarantee: every registered verifier
# returns the same VerifyOutcome field set no matter which of the 55 original
# commands used to own the kind.


def test_json_shape_identical_across_kinds(tmp_path: Path) -> None:
    register_default_verifiers()
    bom_outcome = dispatch_verify(_write_bom(tmp_path))
    bundle_outcome = dispatch_verify(_write_receipt_bundle(tmp_path))

    outcome_fields = {f.name for f in fields(VerifyOutcome)}
    assert outcome_fields == {"kind", "ok", "exit_code", "message", "detail"}
    assert {f.name for f in fields(bom_outcome)} == outcome_fields
    assert {f.name for f in fields(bundle_outcome)} == outcome_fields


# ---------------------------------------------------------------------------
# Dispatcher mechanics: kind-field precedence and registry duplicate-safety
# ---------------------------------------------------------------------------


def test_kind_field_takes_precedence_over_sniffing(tmp_path: Path) -> None:
    sniff_calls: list[str] = []

    def _sniff_never(_path: Path, _payload: dict | None) -> bool:
        sniff_calls.append("called")
        return False

    def _verify_stub(_path: Path) -> VerifyOutcome:
        return VerifyOutcome(kind="stub", ok=True, exit_code=0, message="stubbed")

    register_verifier(VerifierSpec(kind="stub", sniff=_sniff_never, verify=_verify_stub))
    artefact = tmp_path / "artefact.json"
    artefact.write_text(json.dumps({"kind": "stub", "payload": 1}), encoding="utf-8")

    assert detect_kind(artefact) == "stub"
    assert sniff_calls == []  # an explicit `kind` field short-circuits sniffing entirely


def test_duplicate_kind_registration_is_rejected() -> None:
    def _sniff_false(_path: Path, _payload: dict | None) -> bool:
        return False

    def _verify_noop(_path: Path) -> VerifyOutcome:
        return VerifyOutcome(kind="dupe", ok=True, exit_code=0, message="")

    register_verifier(VerifierSpec(kind="dupe", sniff=_sniff_false, verify=_verify_noop))
    with pytest.raises(DuplicateVerifierKindError):
        register_verifier(VerifierSpec(kind="dupe", sniff=_sniff_false, verify=_verify_noop))
