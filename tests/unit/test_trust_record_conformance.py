"""Executable TRACE v0.2 conformance harness (issue #4764).

Distinct from ``tests/unit/test_trust_record_format_vectors.py``, which
re-verifies the committed vectors' own signatures byte-for-byte. This module
checks them against sources this producer does not control:

- the vendored upstream JSON Schema (``schemas/trace-spec/0.2/trace-v0.2.json``),
  via ``jsonschema``;
- ``bernstein.core.observability.trust_record``'s own public
  ``sign_trust_record``/``verify_trust_record`` pair, round-tripped
  independently of whatever the emitter itself produced;
- the reference executable conformance suite, ``agentrust-trace-tests``
  (``trace-tests verify``), when it can be resolved.

``agentrust-trace-tests`` is deliberately NOT a project dependency (dev or
otherwise): pinning it would mean every contributor's ``uv sync`` reaches out
for a package whose only job is this one optional cross-check. Instead this
module resolves it on demand via ``uv run --with``, which needs network
access; the test that does so is skipped (not failed) when that resolution
does not succeed, and the last known-good invocation and result are recorded
in ``schemas/trace-spec/README.md`` so the check has a paper trail even when
it cannot run in a given environment.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from bernstein.core.observability.trust_record import TrustRecordEmitter, sign_trust_record, verify_trust_record

_REPO_ROOT = Path(__file__).resolve().parents[2]
_VECTORS = _REPO_ROOT / "tests" / "fixtures" / "trust-record-vectors"
_SCHEMA_PATH = _REPO_ROOT / "schemas" / "trace-spec" / "0.2" / "trace-v0.2.json"
_PUBKEY = _VECTORS / "trust-record-vectors-key.pem"

_VECTOR_NAMES: tuple[str, ...] = (
    "single-execution-trust-record.json",
    "delegated-parent-trust-record.json",
    "delegated-child-trust-record.json",
    "aggregate-trust-record.json",
)

#: Frozen fixture clock in ``_build_trust_record_vectors.py`` starts at this
#: epoch; the vectors carry ``iat`` values a few seconds after it. A
#: freshness-checking verifier (e.g. ``trace-tests``, or a hypothetical one
#: added to this repo) needs a correspondingly large window to accept them
#: as "fresh" long after this test suite was written.
_FIXTURE_MAX_AGE_SECONDS = 999_999_999_999


def _load(name: str) -> dict[str, Any]:
    return json.loads((_VECTORS / name).read_text(encoding="utf-8"))


def _schema() -> dict[str, Any]:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Vendored schema
# ---------------------------------------------------------------------------


def test_vendored_schema_file_is_present_and_is_the_pinned_trace_v0_2_schema() -> None:
    schema = _schema()
    assert schema["title"] == "TRACE Trust Record"
    assert schema["$id"] == "https://agentrust-io.com/schema/trace-v0.2.json"


class TestEveryVectorConformsToTheVendoredSchema:
    @pytest.mark.parametrize("name", _VECTOR_NAMES)
    def test_vector_validates(self, name: str) -> None:
        jsonschema.validate(_load(name), _schema())


# ---------------------------------------------------------------------------
# sign_trust_record / verify_trust_record round trip (the repo's own
# sign/verify API, not the reference implementation's)
# ---------------------------------------------------------------------------


class TestSignVerifyRoundTrip:
    @pytest.mark.parametrize("name", _VECTOR_NAMES)
    def test_committed_vector_verifies_against_the_pinned_public_key(self, name: str) -> None:
        doc = _load(name)
        assert verify_trust_record(doc, _PUBKEY.read_bytes()) is True

    def test_a_freshly_signed_record_verifies(self) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        key = Ed25519PrivateKey.generate()
        private_pem = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
        public_pem = key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )

        body = _load("single-execution-trust-record.json")
        del body["signature"]
        signed = sign_trust_record(body, private_pem)

        assert verify_trust_record(signed, public_pem) is True

    def test_a_tampered_field_fails_verification(self) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        key = Ed25519PrivateKey.generate()
        private_pem = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
        public_pem = key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )

        body = _load("single-execution-trust-record.json")
        del body["signature"]
        signed = sign_trust_record(body, private_pem)

        signed["data_class"] = "public"
        assert verify_trust_record(signed, public_pem) is False

    def test_sign_trust_record_refuses_a_record_that_already_carries_a_signature(self) -> None:
        with pytest.raises(ValueError, match="signature"):
            sign_trust_record({"signature": "already-there"}, b"irrelevant")

    def test_verify_trust_record_refuses_a_record_with_no_signature(self) -> None:
        with pytest.raises(ValueError, match="signature"):
            verify_trust_record({"eat_profile": "x"}, _PUBKEY.read_bytes())


# ---------------------------------------------------------------------------
# Install-identity JWK never carries private key material
# ---------------------------------------------------------------------------

#: RFC 7517/RFC 8037 private-key JWK members. None of these may ever appear
#: on a ``cnf.jwk`` this producer emits -- it is a public confirmation key.
_PRIVATE_JWK_MEMBERS: tuple[str, ...] = ("d", "p", "q", "dp", "dq", "qi", "k")


class TestInstallIdentityJwkNeverCarriesPrivateKeyMaterial:
    @pytest.mark.parametrize("name", _VECTOR_NAMES)
    def test_committed_vector_jwk_has_no_private_members(self, name: str) -> None:
        jwk = _load(name)["cnf"]["jwk"]
        for member in _PRIVATE_JWK_MEMBERS:
            assert member not in jwk, f"cnf.jwk must never carry {member!r} (private key material)"

    def test_freshly_emitted_record_jwk_has_no_private_members(self, tmp_path: Path) -> None:
        from bernstein.core.replay.journal import EventJournal

        journal = EventJournal("conformance-jwk-check", tmp_path / ".sdd")
        journal.record(
            "run_started",
            model_provider="anthropic",
            model_id="claude-sonnet-5",
            gate_config={"rules": []},
        )
        journal.record("run_completed", status="ok")

        emitter = TrustRecordEmitter(
            install_rev_getter=lambda: "conformance-check",
            get_installed_digest=lambda: "sha256:" + "0" * 64,
        )
        parsed = json.loads(emitter.emit_trust_record(journal.path, "run-1", "exec-1"))

        jwk = parsed["cnf"]["jwk"]
        for member in _PRIVATE_JWK_MEMBERS:
            assert member not in jwk


# ---------------------------------------------------------------------------
# Regression: build the minimal per-execution record from a fresh fixture
# journal and validate it against the vendored schema -- catches a schema
# drift the four committed vectors happen not to exercise.
# ---------------------------------------------------------------------------


class TestMinimalRecordFromAFixtureJournalConformsToTheSchema:
    def test_minimal_journal_produces_a_schema_valid_record(self, tmp_path: Path) -> None:
        from bernstein.core.replay.journal import EventJournal

        journal = EventJournal("conformance-minimal", tmp_path / ".sdd")
        # The absolute minimum: one event naming the two fields that have no
        # honest default (model, gate_config), nothing else.
        journal.record(
            "run_started",
            model_provider="anthropic",
            model_id="claude-sonnet-5",
            gate_config={"rules": []},
        )

        emitter = TrustRecordEmitter(
            install_rev_getter=lambda: "conformance-check",
            get_installed_digest=lambda: "sha256:" + "0" * 64,
        )
        parsed = json.loads(emitter.emit_trust_record(journal.path, "run-1", "exec-1"))

        jsonschema.validate(parsed, _schema())
        # The minimal case has no delegation and no produced artifacts.
        assert "delegation" not in parsed
        assert "references" not in parsed

    def test_minimal_delegated_child_record_conforms_to_the_schema(self, tmp_path: Path) -> None:
        from bernstein.core.replay.journal import EventJournal

        parent_journal = EventJournal("conformance-minimal-parent", tmp_path / ".sdd")
        parent_journal.record(
            "run_started", model_provider="anthropic", model_id="claude-sonnet-5", gate_config={"rules": []}
        )
        child_journal = EventJournal("conformance-minimal-child", tmp_path / ".sdd")
        child_journal.record(
            "run_started", model_provider="anthropic", model_id="claude-haiku-5", gate_config={"rules": []}
        )

        emitter = TrustRecordEmitter(
            install_rev_getter=lambda: "conformance-check",
            get_installed_digest=lambda: "sha256:" + "0" * 64,
        )
        parent_output = emitter.emit_trust_record(parent_journal.path, "run-1", "exec-parent")
        child_output = emitter.emit_trust_record(
            child_journal.path,
            "run-1",
            "exec-child",
            parent_record=parent_output,
            credential_id="cred-1",
        )

        jsonschema.validate(json.loads(child_output), _schema())

    def test_minimal_aggregate_record_conforms_to_the_schema(self, tmp_path: Path) -> None:
        from bernstein.core.replay.journal import EventJournal

        parent_journal = EventJournal("conformance-minimal-agg-parent", tmp_path / ".sdd")
        parent_journal.record(
            "run_started",
            model_provider="anthropic",
            model_id="claude-sonnet-5",
            data_class="internal",
            gate_config={"rules": ["a"]},
        )
        child_journal = EventJournal("conformance-minimal-agg-child", tmp_path / ".sdd")
        child_journal.record(
            "run_started",
            model_provider="anthropic",
            model_id="claude-haiku-5",
            data_class="internal",
            gate_config={"rules": ["b"]},
        )

        emitter = TrustRecordEmitter(
            install_rev_getter=lambda: "conformance-check",
            get_installed_digest=lambda: "sha256:" + "0" * 64,
        )
        parent_output = emitter.emit_trust_record(parent_journal.path, "run-1", "exec-parent")
        child_output = emitter.emit_trust_record(
            child_journal.path,
            "run-1",
            "exec-child",
            parent_record=parent_output,
            credential_id="cred-1",
        )
        aggregate_output = emitter.emit_aggregate_trust_record("run-1", [parent_output, child_output])

        jsonschema.validate(json.loads(aggregate_output), _schema())


# ---------------------------------------------------------------------------
# Reference executable conformance suite (agentrust-trace-tests / trace-tests)
# ---------------------------------------------------------------------------


def _resolve_trace_tests() -> str | None:
    """Return a ``uv`` command prefix that runs ``trace-tests``, or ``None``.

    Never raises: any failure to resolve the CLI (no ``uv`` on PATH, no
    network, PyPI unreachable) means "skip this check", not "fail the
    suite" -- this dependency is deliberately not vendored (see the module
    docstring), so its unavailability is an environment fact, not a defect.
    """
    if shutil.which("uv") is None:
        return None
    try:
        probe = subprocess.run(
            ["uv", "run", "--with", "agentrust-trace-tests==0.5.1", "trace-tests", "--version"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if probe.returncode != 0:
        return None
    return "ok"


@pytest.mark.skipif(
    _resolve_trace_tests() is None,
    reason=(
        "agentrust-trace-tests could not be resolved (no 'uv' on PATH, or no network "
        "access to PyPI); this dependency is deliberately not vendored -- see this "
        "module's docstring and schemas/trace-spec/README.md"
    ),
)
class TestReferenceConformanceSuite:
    """Runs the actual upstream ``trace-tests verify`` CLI against every vector.

    Recorded, passing invocation (schemas/trace-spec/README.md keeps the
    same line for humans who cannot run this test in their environment)::

        uv run --with agentrust-trace-tests==0.5.1 trace-tests verify \\
            --record tests/fixtures/trust-record-vectors/<name>.json \\
            --level 0 --max-age 999999999999

    Result at the time of writing: ``PASS (8 checks, 0 skipped)`` for all
    four vectors.
    """

    @pytest.mark.parametrize("name", _VECTOR_NAMES)
    def test_vector_passes_level_0(self, name: str) -> None:
        result = subprocess.run(
            [
                "uv",
                "run",
                "--with",
                "agentrust-trace-tests==0.5.1",
                "trace-tests",
                "verify",
                "--record",
                str(_VECTORS / name),
                "--level",
                "0",
                "--max-age",
                str(_FIXTURE_MAX_AGE_SECONDS),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert "Result: PASS" in result.stdout, result.stdout + result.stderr
        assert "TR-SIG" in result.stdout
        # trace-tests exits 0 on PASS; a non-zero exit with "Result: PASS" in
        # the text would itself be worth knowing about.
        assert result.returncode == 0, result.stdout + result.stderr
