"""Tests for MCP server signing + scanning + policy enforcement.

Covers the four scenarios called out by the parent ticket plus the
OpenClaw CVE pattern matches in the static scanner:

- (a) signed server with trusted publisher passes verification
- (b) tampered signature fails with structured verdict
- (c) unsigned server in warn-only mode logs and ticks counter
- (d) unsigned server in strict mode raises ``MCPVerificationError``
"""

from __future__ import annotations

import base64
import logging

import pytest
from cryptography.hazmat.primitives import serialization

from bernstein.core.protocols.mcp.mcp_scanner import (
    DEFAULT_KNOWN_BAD_PACKAGES,
    ScannerSeverity,
    scan_dependency_diff,
    scan_mcp_bundle,
)
from bernstein.core.protocols.mcp.mcp_signing_policy import (
    ENV_ALLOW_UNSIGNED,
    MCPSigningPolicy,
    enforce_mcp_server_load,
    reset_metrics_for_test,
    unsigned_loaded_counter_value,
)
from bernstein.core.protocols.mcp.mcp_verifier import (
    MCPVerificationError,
    VerificationVerdict,
    canonicalize_manifest,
    parse_manifest,
    verify_mcp_server,
)
from bernstein.core.security.agent_card_signer import generate_ed25519_keypair

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


def _make_signed_manifest(
    *,
    name: str = "example-mcp",
    version: str = "1.0.0",
    publisher_name: str = "example.com",
) -> tuple[str, str, str, bytes]:
    """Return ``(manifest_yaml, signature_b64, fingerprint, public_pem)``."""
    private_pem, public_pem = generate_ed25519_keypair()
    # Use a stable, recognisable fingerprint string (the verifier treats
    # the fingerprint as opaque except for the ed25519/ prefix).
    fingerprint = f"ed25519/{name.replace('-', '')}fingerprint00"
    manifest_json = (
        f'{{"name": "{name}", "version": "{version}", "publisher": '
        f'{{"name": "{publisher_name}", "fingerprint": "{fingerprint}"}}, '
        f'"content_hash": ""}}'
    )
    manifest = parse_manifest(manifest_json)
    signing_input = canonicalize_manifest(manifest)
    private_key = serialization.load_pem_private_key(private_pem, password=None)
    signature = private_key.sign(signing_input)  # type: ignore[union-attr]
    sig_b64 = base64.b64encode(signature).decode("ascii")
    return manifest_json, sig_b64, fingerprint, public_pem


# ---------------------------------------------------------------------------
# parse_manifest
# ---------------------------------------------------------------------------


class TestParseManifest:
    def test_parses_valid_json_manifest(self) -> None:
        manifest = parse_manifest(
            '{"name": "ex", "version": "1.0", "publisher": {"name": "p", "fingerprint": "ed25519/abc"}}'
        )
        assert manifest.name == "ex"
        assert manifest.version == "1.0"
        assert manifest.publisher_fingerprint == "ed25519/abc"

    def test_rejects_non_mapping_root(self) -> None:
        with pytest.raises(MCPVerificationError) as exc:
            parse_manifest('["not", "a", "map"]')
        assert exc.value.verdict == VerificationVerdict.BAD_MANIFEST

    def test_rejects_missing_name(self) -> None:
        with pytest.raises(MCPVerificationError) as exc:
            parse_manifest('{"version": "1.0", "publisher": {"name": "p", "fingerprint": "ed25519/abc"}}')
        assert exc.value.verdict == VerificationVerdict.BAD_MANIFEST

    def test_rejects_non_ed25519_fingerprint(self) -> None:
        with pytest.raises(MCPVerificationError) as exc:
            parse_manifest('{"name": "ex", "version": "1.0", "publisher": {"name": "p", "fingerprint": "rsa/abc"}}')
        assert exc.value.verdict == VerificationVerdict.BAD_MANIFEST

    def test_rejects_bad_content_hash_prefix(self) -> None:
        with pytest.raises(MCPVerificationError) as exc:
            parse_manifest(
                '{"name": "ex", "version": "1.0", "publisher": '
                '{"name": "p", "fingerprint": "ed25519/abc"}, '
                '"content_hash": "md5/deadbeef"}'
            )
        assert exc.value.verdict == VerificationVerdict.BAD_MANIFEST


class TestCanonicalizeManifest:
    def test_is_deterministic(self) -> None:
        manifest_json = '{"name": "ex", "version": "1.0", "publisher": {"name": "p", "fingerprint": "ed25519/abc"}}'
        m = parse_manifest(manifest_json)
        assert canonicalize_manifest(m) == canonicalize_manifest(m)

    def test_includes_typ_binding(self) -> None:
        m = parse_manifest('{"name": "ex", "version": "1.0", "publisher": {"name": "p", "fingerprint": "ed25519/abc"}}')
        # The typ binding prevents cross-context signature replay.
        assert b"mcp-server-manifest+ed25519" in canonicalize_manifest(m)


# ---------------------------------------------------------------------------
# verify_mcp_server - the four parent-required scenarios
# ---------------------------------------------------------------------------


class TestVerifyMCPServer:
    def test_signed_server_passes(self) -> None:
        """(a) signed server with trusted publisher → ok=True."""
        manifest, sig, fp, pem = _make_signed_manifest()
        result = verify_mcp_server(
            manifest_yaml=manifest,
            signature_b64=sig,
            publisher_public_key_pem=pem,
            trusted_publishers={fp},
        )
        assert result.ok is True
        assert result.verdict == VerificationVerdict.OK
        assert result.publisher_fingerprint == fp

    def test_tampered_signature_fails(self) -> None:
        """(b) tampered signature → ok=False, BAD_SIGNATURE."""
        manifest, sig, fp, pem = _make_signed_manifest()
        # Flip a byte in the signature so verification mathematically fails
        sig_bytes = bytearray(base64.b64decode(sig))
        sig_bytes[0] ^= 0xFF
        tampered_sig = base64.b64encode(bytes(sig_bytes)).decode("ascii")

        result = verify_mcp_server(
            manifest_yaml=manifest,
            signature_b64=tampered_sig,
            publisher_public_key_pem=pem,
            trusted_publishers={fp},
        )
        assert result.ok is False
        assert result.verdict == VerificationVerdict.BAD_SIGNATURE

    def test_untrusted_publisher_refused(self) -> None:
        """Even valid signature → UNTRUSTED_PUBLISHER if not allowlisted."""
        manifest, sig, _fp, pem = _make_signed_manifest()
        result = verify_mcp_server(
            manifest_yaml=manifest,
            signature_b64=sig,
            publisher_public_key_pem=pem,
            trusted_publishers={"ed25519/some-other-fingerprint"},
        )
        assert result.ok is False
        assert result.verdict == VerificationVerdict.UNTRUSTED_PUBLISHER

    def test_empty_signature_marks_unsigned(self) -> None:
        """Empty signature_b64 → UNSIGNED verdict."""
        manifest, _sig, fp, pem = _make_signed_manifest()
        result = verify_mcp_server(
            manifest_yaml=manifest,
            signature_b64="",
            publisher_public_key_pem=pem,
            trusted_publishers={fp},
        )
        assert result.ok is False
        assert result.verdict == VerificationVerdict.UNSIGNED

    @pytest.mark.parametrize(
        ("signature", "expected"),
        [
            (None, VerificationVerdict.UNSIGNED),
            (b"c2ln", VerificationVerdict.BAD_SIGNATURE),
            (12345, VerificationVerdict.BAD_SIGNATURE),
        ],
    )
    def test_non_string_signature_returns_a_verdict_instead_of_raising(
        self, signature: object, expected: VerificationVerdict
    ) -> None:
        """The verifier's contract is a verdict; a type violation must not escape as an exception.

        ``signature_b64`` is annotated ``str``, but it carries a value that
        crossed a trust boundary - a parsed JSON ``null``, or the bytes from a
        ``.sig`` file read without ``.decode()``. Callers outside this module
        are not type-checked against it. Before the guard, ``.strip()`` raised
        ``AttributeError`` and a caller written to branch on the verdict got an
        exception instead, in the one code path whose whole purpose is to fail
        closed.

        ``None`` is a signature never supplied and stays UNSIGNED; anything
        else was supplied and cannot be read, which is BAD_SIGNATURE. The
        distinction is load-bearing: ``mcp_signing_policy`` allows UNSIGNED
        under a warn-only policy and never allows BAD_SIGNATURE.
        """
        manifest, _sig, fp, pem = _make_signed_manifest()
        result = verify_mcp_server(
            manifest_yaml=manifest,
            signature_b64=signature,  # type: ignore[arg-type]  # the point of the test
            publisher_public_key_pem=pem,
            trusted_publishers={fp},
        )
        assert result.ok is False
        assert result.verdict == expected

    def test_content_hash_mismatch(self) -> None:
        """Manifest content_hash + non-matching bundle → mismatch verdict."""
        private_pem, public_pem = generate_ed25519_keypair()
        fp = "ed25519/contenthashtest"
        manifest_json = (
            f'{{"name": "ex", "version": "1.0", "publisher": '
            f'{{"name": "p", "fingerprint": "{fp}"}}, '
            f'"content_hash": "sha256/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
            f'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}}'
        )
        m = parse_manifest(manifest_json)
        signing_input = canonicalize_manifest(m)
        private_key = serialization.load_pem_private_key(private_pem, password=None)
        sig = base64.b64encode(private_key.sign(signing_input)).decode("ascii")  # type: ignore[union-attr]

        result = verify_mcp_server(
            manifest_yaml=manifest_json,
            signature_b64=sig,
            publisher_public_key_pem=public_pem,
            trusted_publishers={fp},
            bundle_bytes=b"different content",
        )
        assert result.ok is False
        assert result.verdict == VerificationVerdict.CONTENT_HASH_MISMATCH


# ---------------------------------------------------------------------------
# Static scanner - OpenClaw CVE patterns
# ---------------------------------------------------------------------------


class TestMCPScanner:
    def test_path_traversal_pattern_flagged(self) -> None:
        findings = scan_mcp_bundle(
            bundle_files={
                "tools/read.py": (
                    "def read(p):\n    with open(os.path.join('/srv', p)) as f:\n        return f.read()\n"
                )
            },
        )
        assert any(f.rule == "path_traversal" for f in findings)

    def test_path_traversal_safe_form_passes(self) -> None:
        findings = scan_mcp_bundle(
            bundle_files={
                "tools/read.py": (
                    "def read(p):\n    target = (Path('/srv') / p).resolve()\n    return target.read_text()\n"
                )
            },
        )
        # The .resolve() pattern suppresses the path_traversal finding
        assert not any(f.rule == "path_traversal" for f in findings)

    def test_shell_injection_pattern_flagged(self) -> None:
        findings = scan_mcp_bundle(
            bundle_files={"tools/exec.py": ("def run(cmd):\n    subprocess.run(cmd, shell=True)\n")},
        )
        critical = [f for f in findings if f.rule == "shell_injection"]
        assert critical
        assert critical[0].severity == ScannerSeverity.CRITICAL
        assert critical[0].cwe == "CWE-78"

    def test_oauth_callback_rce_pattern_flagged(self) -> None:
        findings = scan_mcp_bundle(
            bundle_files={
                "auth/callback.py": (
                    "def callback(request):\n"
                    "    redirect_uri = request.query['redirect_uri']\n"
                    "    return RedirectResponse(redirect_uri)\n"
                )
            },
        )
        assert any(f.rule == "oauth_callback_rce" for f in findings)

    def test_oauth_callback_safe_form_passes(self) -> None:
        findings = scan_mcp_bundle(
            bundle_files={
                "auth/callback.py": (
                    "def callback(request):\n"
                    "    redirect_uri = request.query['redirect_uri']\n"
                    "    if redirect_uri not in ALLOWED_REDIRECTS:\n"
                    "        raise BadRequest()\n"
                )
            },
        )
        assert not any(f.rule == "oauth_callback_rce" for f in findings)

    def test_scope_escalation_pattern_flagged(self) -> None:
        findings = scan_mcp_bundle(
            bundle_files={
                "auth/refresh.py": ("def refresh(token):\n    return refresh_with(token, scope='admin write')\n")
            },
        )
        assert any(f.rule == "scope_escalation" for f in findings)

    def test_known_bad_package_flagged(self) -> None:
        findings = scan_mcp_bundle(
            bundle_files={"src/init.py": "x = 1\n"},
            package_name="mcp-remote",
        )
        assert any(f.rule == "known_bad_package" for f in findings)
        # The seed list MUST include the publicly-tracked mcp-remote CVE.
        assert "mcp-remote" in DEFAULT_KNOWN_BAD_PACKAGES

    def test_clean_bundle_no_findings(self) -> None:
        findings = scan_mcp_bundle(
            bundle_files={"src/server.py": ("def hello():\n    return {'msg': 'hi'}\n")},
        )
        assert findings == []

    def test_dependency_hash_mismatch_flagged(self) -> None:
        findings = scan_dependency_diff(
            declared_hashes={"foo": "sha256/abc"},
            locked_hashes={"foo": "sha256/def"},
        )
        assert any(f.rule == "dependency_hash_mismatch" for f in findings)
        assert findings[0].severity == ScannerSeverity.HIGH


# ---------------------------------------------------------------------------
# Policy enforcement - strict vs warn-only
# ---------------------------------------------------------------------------


class TestEnforceMCPServerLoad:
    def setup_method(self) -> None:
        reset_metrics_for_test()

    def test_signed_trusted_passes_strict(self) -> None:
        manifest, sig, fp, pem = _make_signed_manifest()
        policy = MCPSigningPolicy(
            strict=True,
            trusted_publishers=frozenset({fp}),
            publisher_keys={fp: pem},
        )
        decision = enforce_mcp_server_load(
            server_name="example",
            manifest_yaml=manifest,
            signature_b64=sig,
            bundle_files={"src/init.py": "x = 1\n"},
            policy=policy,
        )
        assert decision.allowed is True
        assert decision.verification.ok is True

    def test_unsigned_strict_raises(self) -> None:
        """(d) Unsigned server in strict mode → MCPVerificationError."""
        manifest, _sig, fp, pem = _make_signed_manifest()
        policy = MCPSigningPolicy(
            strict=True,
            trusted_publishers=frozenset({fp}),
            publisher_keys={fp: pem},
        )
        with pytest.raises(MCPVerificationError) as exc:
            enforce_mcp_server_load(
                server_name="example",
                manifest_yaml=manifest,
                signature_b64="",
                bundle_files=None,
                policy=policy,
            )
        assert exc.value.verdict == VerificationVerdict.UNSIGNED
        # Remediation message must cite the verify CLI verb
        assert "bernstein mcp verify" in str(exc.value)

    def test_unsigned_warn_only_logs_and_counts(self, caplog: pytest.LogCaptureFixture) -> None:
        """(c) Unsigned server in warn-only mode → log + counter tick."""
        manifest, _sig, fp, pem = _make_signed_manifest()
        policy = MCPSigningPolicy(
            strict=False,
            trusted_publishers=frozenset({fp}),
            publisher_keys={fp: pem},
        )
        before = unsigned_loaded_counter_value()
        with caplog.at_level(logging.WARNING):
            decision = enforce_mcp_server_load(
                server_name="example",
                manifest_yaml=manifest,
                signature_b64="",
                bundle_files=None,
                policy=policy,
            )
        assert decision.allowed is True
        assert unsigned_loaded_counter_value() == before + 1
        assert any("warn-only" in r.message for r in caplog.records)

    def test_tampered_signature_strict_raises(self) -> None:
        """(b) tampered signature in strict mode → raise."""
        manifest, sig, fp, pem = _make_signed_manifest()
        sig_bytes = bytearray(base64.b64decode(sig))
        sig_bytes[0] ^= 0xFF
        tampered = base64.b64encode(bytes(sig_bytes)).decode("ascii")
        policy = MCPSigningPolicy(
            strict=True,
            trusted_publishers=frozenset({fp}),
            publisher_keys={fp: pem},
        )
        with pytest.raises(MCPVerificationError) as exc:
            enforce_mcp_server_load(
                server_name="example",
                manifest_yaml=manifest,
                signature_b64=tampered,
                bundle_files=None,
                policy=policy,
            )
        assert exc.value.verdict == VerificationVerdict.BAD_SIGNATURE

    def test_critical_finding_blocks_strict_load(self) -> None:
        """Even with a valid signature, CRITICAL scanner finding → strict deny."""
        manifest, sig, fp, pem = _make_signed_manifest()
        policy = MCPSigningPolicy(
            strict=True,
            trusted_publishers=frozenset({fp}),
            publisher_keys={fp: pem},
        )
        with pytest.raises(MCPVerificationError):
            enforce_mcp_server_load(
                server_name="example",
                manifest_yaml=manifest,
                signature_b64=sig,
                bundle_files={"tools/exec.py": "subprocess.run(cmd, shell=True)\n"},
                policy=policy,
            )

    def test_critical_finding_warns_only_in_warn_mode(self, caplog: pytest.LogCaptureFixture) -> None:
        manifest, sig, fp, pem = _make_signed_manifest()
        policy = MCPSigningPolicy(
            strict=False,
            trusted_publishers=frozenset({fp}),
            publisher_keys={fp: pem},
        )
        with caplog.at_level(logging.WARNING):
            decision = enforce_mcp_server_load(
                server_name="example",
                manifest_yaml=manifest,
                signature_b64=sig,
                bundle_files={"tools/exec.py": "subprocess.run(cmd, shell=True)\n"},
                policy=policy,
            )
        assert decision.allowed is True
        assert any(f.severity == ScannerSeverity.CRITICAL for f in decision.scanner_findings)
        assert any("CRITICAL" in r.message for r in caplog.records)

    def test_env_override_forces_warn_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_ALLOW_UNSIGNED, "true")
        policy = MCPSigningPolicy.from_config(
            config={"allow_unsigned": False},
        )
        assert policy.strict is False

    def test_config_allow_unsigned_flag(self) -> None:
        policy = MCPSigningPolicy.from_config(
            config={"allow_unsigned": True, "trusted_publishers": ["ed25519/abc"]},
        )
        assert policy.strict is False
        assert "ed25519/abc" in policy.trusted_publishers

    def test_unknown_publisher_key_falls_to_untrusted(self) -> None:
        """Manifest's publisher fingerprint not in publisher_keys → UNTRUSTED."""
        manifest, sig, _fp, _pem = _make_signed_manifest()
        # No publisher_keys - verifier sees empty PEM, falls through to
        # UNTRUSTED_PUBLISHER (since the fingerprint isn't in the
        # trusted_publishers set either).
        policy = MCPSigningPolicy(
            strict=False,
            trusted_publishers=frozenset(),
            publisher_keys={},
        )
        decision = enforce_mcp_server_load(
            server_name="example",
            manifest_yaml=manifest,
            signature_b64=sig,
            bundle_files=None,
            policy=policy,
        )
        assert decision.allowed is True  # warn-only
        assert decision.verification.verdict == VerificationVerdict.UNTRUSTED_PUBLISHER
