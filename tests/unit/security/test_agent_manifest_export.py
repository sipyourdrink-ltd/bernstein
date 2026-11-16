"""Pinned composition-only contract; no CLI exit code substitutes for runtime appraisal."""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein.core.security.agent_card_signer import canonicalize_jcs
from bernstein.core.security.agent_manifest_export import (
    UNBOUND_SLOTS,
    CompositionDeclarations,
    CompositionManifestExporter,
    build_composition_export,
    verify_availability_companion,
)
from bernstein.core.security.key_custody import public_key_jwk_for

sdk = pytest.importorskip("agent_manifest")


class FixtureSigner:
    """A fixture key only; production custody stays behind KMSAdapter."""

    def __init__(self):
        self.key = Ed25519PrivateKey.from_private_bytes(b"m" * 32)

    def sign(self, payload):
        return self.key.sign(payload)

    def public_key_jwk(self):
        return public_key_jwk_for(self.key.public_key())

    def public_pem(self):
        return self.key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )


@pytest.fixture
def declarations():
    now = datetime.now(UTC).replace(microsecond=0)
    return dict(
        manifest_id="019c6e27-e55b-73d1-87d8-4e01f1f75043",
        agent_id="spiffe://example.test/worker/backend",
        issuer="spiffe://example.test/installation",
        version="0.1",
        issued_at=now.isoformat(),
        expires_at=(now + timedelta(hours=2)).isoformat(),
        signed_at=now.isoformat(),
        prompt_bound_at=now.isoformat(),
        prompt_version="operator-revision-7",
        classification="internal",
        key_type="software",
        availability_reasons={
            name: f"Operator declares {name} unavailable at this composition boundary" for name in UNBOUND_SLOTS
        },
    )


def _build(declarations, prompt=b"rendered role and task composition"):
    return build_composition_export(prompt, CompositionDeclarations.model_validate(declarations), FixtureSigner())


def _verify(export, prompt_hash=None):
    manifest = json.loads(export.manifest_bytes)
    jwk = FixtureSigner().public_key_jwk()
    raw = base64.urlsafe_b64decode(jwk["x"] + "==")
    return sdk.verify_manifest(
        manifest,
        sdk.VerificationContext(
            trusted_keys={hashlib.sha256(raw).hexdigest(): jwk["x"]},
            system_prompt_hash=prompt_hash,
            strict_artifact_verification=True,
        ),
        sdk.RevocationStore(),
    )


def test_deterministic_and_upstream_schema(declarations):
    a, b = _build(declarations), _build(declarations)
    assert a.unsigned_bytes == b.unsigned_bytes
    assert a.manifest_bytes == b.manifest_bytes
    assert a.availability_bytes == b.availability_bytes
    doc = json.loads(a.manifest_bytes)
    sdk.Manifest.model_validate(doc)
    assert doc["profile"] == "composition-only"
    assert set(doc["unbound_artifacts"]) == set(UNBOUND_SLOTS)
    assert set(doc["artifacts"]) == {"system_prompt"}
    assert doc["artifacts"]["system_prompt"]["classification"] == "internal"
    assert "availability_reasons" not in doc
    assert doc["issued_at"] == declarations["issued_at"].replace("+00:00", "Z")


def test_runtime_appraisal_preserves_incomplete_and_detects_changed_prompt(declarations):
    export = _build(declarations)
    match = _verify(export, "sha256:" + hashlib.sha256(b"rendered role and task composition").hexdigest())
    assert match.signature_verified
    assert match.fields_verified.system_prompt == sdk.FieldResult.MATCH
    assert match.result == sdk.OverallResult.INCOMPLETE
    mismatch = _verify(export, "sha256:" + hashlib.sha256(b"Rendered role and task composition").hexdigest())
    assert mismatch.result == sdk.OverallResult.MISMATCH
    assert mismatch.fields_verified.system_prompt == sdk.FieldResult.MISMATCH
    missing = _verify(export)
    assert missing.result == sdk.OverallResult.INCOMPLETE
    assert any("artifact bindings NOT verified" in warning for warning in missing.warnings)


def test_companion_authenticates_exact_signed_manifest_and_reasons(declarations):
    export = _build(declarations)
    assert verify_availability_companion(export.manifest_bytes, export.availability_bytes, FixtureSigner().public_pem())
    companion = json.loads(export.availability_bytes)
    assert companion["manifest_digest"] == "sha256:" + hashlib.sha256(export.manifest_bytes).hexdigest()
    assert companion["availability_reasons"] == declarations["availability_reasons"]
    companion["availability_reasons"]["model_identity"] = "invented replacement"
    assert not verify_availability_companion(
        export.manifest_bytes, canonicalize_jcs(companion), FixtureSigner().public_pem()
    )
    other = dict(declarations, prompt_version="other-version")
    assert not verify_availability_companion(
        _build(other).manifest_bytes, export.availability_bytes, FixtureSigner().public_pem()
    )
    assert not verify_availability_companion(b"[]", b"{}", FixtureSigner().public_pem())


@pytest.mark.parametrize(
    "field",
    [
        "manifest_id",
        "agent_id",
        "issuer",
        "version",
        "issued_at",
        "expires_at",
        "signed_at",
        "prompt_bound_at",
        "prompt_version",
        "classification",
        "key_type",
        "availability_reasons",
    ],
)
def test_no_declaration_is_inferred(declarations, field):
    declarations.pop(field)
    with pytest.raises(ValueError):
        _build(declarations)


@pytest.mark.parametrize(
    "update",
    [
        {"version": "0.2"},
        {"classification": "unknown"},
        {"prompt_version": " "},
        {"issued_at": "2026-11-15T08:00:00"},
        {"unsupported_profile": True},
        {"expires_at": "2020-01-01T00:00:00Z"},
        {"availability_reasons": {}},
    ],
)
def test_invalid_or_unknown_declarations_fail_closed(declarations, update):
    declarations.update(update)
    with pytest.raises(ValueError):
        _build(declarations)


def test_exact_reasons_no_extra_bound_or_empty_values(declarations):
    for extra, value in [("system_prompt", "bound"), ("alien", "unknown"), ("model_identity", " ")]:
        altered = dict(declarations, availability_reasons=dict(declarations["availability_reasons"], **{extra: value}))
        with pytest.raises(ValueError):
            _build(altered)


def test_exporter_writes_once_under_lineage_and_returns_hash_references(tmp_path, declarations):
    exporter = CompositionManifestExporter(
        tmp_path, lambda hop: CompositionDeclarations.model_validate(declarations), FixtureSigner()
    )
    refs = exporter("worker-1", "composition")
    assert len(refs) == 2
    for ref in refs:
        path = tmp_path / ref["resolver"]
        assert path.is_file()
        assert ref["digest"] == "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(FileExistsError):
        exporter("worker-1", "replacement")
    for unsafe in ["..", "../escape", "/absolute", "a/b", "a\\b"]:
        with pytest.raises(ValueError):
            exporter(unsafe, "composition")


def test_pinned_cli_reports_incomplete_without_runtime_hashes(tmp_path, declarations):
    export = _build(declarations)
    manifest = tmp_path / "manifest.json"
    manifest.write_bytes(export.manifest_bytes)
    public = tmp_path / "public.hex"
    raw = FixtureSigner().key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    public.write_text(raw.hex())
    for flags in [[], ["--signature-only"]]:
        proc = subprocess.run(
            [sys.executable, "-m", "agent_manifest.cli", "verify", str(manifest), "--public-key", str(public), *flags],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "INCOMPLETE" in proc.stdout + proc.stderr


def test_cli_export_is_explicitly_not_runtime_verification(tmp_path, declarations, monkeypatch):
    from click.testing import CliRunner

    from bernstein.cli.commands.manifest_cmd import manifest_group
    from bernstein.core.security.agent_card_keystore import AgentCardKeystore

    monkeypatch.setattr(AgentCardKeystore, "signer", lambda self: FixtureSigner())
    declared = tmp_path / "declarations.json"
    declared.write_text(json.dumps(declarations))
    prompt = tmp_path / "prompt.txt"
    prompt.write_bytes(b"composition\r\nwith exact bytes")
    lineage = tmp_path / "lineage"
    result = CliRunner().invoke(
        manifest_group,
        [
            "export-agent",
            "--declarations",
            str(declared),
            "--prompt",
            str(prompt),
            "--lineage-root",
            str(lineage),
            "--hop-id",
            "worker",
            "--key-dir",
            str(tmp_path / "keys"),
        ],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["runtime_artifacts_verified"] is False
    assert data["status"] == "exported"
    document = json.loads((lineage / data["references"][0]["resolver"]).read_bytes())
    assert document["artifacts"]["system_prompt"]["hash"] == "sha256:" + hashlib.sha256(prompt.read_bytes()).hexdigest()


def test_core_import_keeps_optional_sdk_lazy():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import bernstein.core.security.agent_manifest_export; assert 'agent_manifest' not in sys.modules",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("duration", [timedelta(minutes=59), timedelta(days=366)])
def test_expiry_window_matches_pinned_upstream(declarations, duration):
    issued = datetime.fromisoformat(declarations["issued_at"])
    declarations["expires_at"] = (issued + duration).isoformat()
    with pytest.raises(ValueError):
        _build(declarations)


def test_different_sdk_version_fails_before_signing(declarations, monkeypatch):
    from unittest.mock import Mock

    import bernstein.core.security.agent_manifest_export as module

    monkeypatch.setattr(module, "version", lambda package: "0.13.0")
    signer = Mock()
    with pytest.raises(ValueError, match="requires agent-manifest==0.12.0"):
        build_composition_export(b"prompt", CompositionDeclarations.model_validate(declarations), signer)
    signer.sign.assert_not_called()


def test_wrong_trust_key_rejects_companion(declarations):
    export = _build(declarations)
    other = (
        Ed25519PrivateKey.from_private_bytes(b"n" * 32)
        .public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    assert not verify_availability_companion(export.manifest_bytes, export.availability_bytes, other)
