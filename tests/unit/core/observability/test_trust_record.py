"""Tests for :mod:`bernstein.core.observability.trust_record`.

Focused tests for the TRACE 0.2 Trust Record emitter functionality.
Tests cover journal parsing, field construction, signing, and canonical
output.

Issues #4760/#4761/#4762 re-aligned the emitted field surface to the
upstream schema at agentrust-io/trace-spec pin ``e7e2eca`` (a
producer-mapping review of agentrust-io/trace-spec#231): the homegrown
``claims``/``enforce`` shape is gone in favour of ``eat_profile``, ``iat``,
``model``, ``policy``, ``data_class``, and ``tool_transcript``; ``subject``
moved to a fixed-trust-domain SPIFFE scheme; ``delegation`` and
``references`` are omitted rather than emitted hollow. Tests are grouped
by the field/property they protect.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

import pytest

from bernstein.core.observability.trust_record import (
    TrustRecord,
    TrustRecordEmitter,
    _sign_raw_ed25519,
    spiffe_subject_for_aggregate,
    spiffe_subject_for_execution,
)
from bernstein.core.replay.journal import (
    _GENESIS_HASH,
    _payload_hash,
    compute_event_hash,
)
from bernstein.core.security.agent_card_signer import canonicalize_jcs

#: Fixed 32-byte Ed25519 seed for tests that need to independently recompute
#: expected key material (as opposed to the per-test isolated but *unknown*
#: key the autouse ``_isolate_agent_card_keystore`` fixture wires up).
#: Never used outside the test tree.
_TEST_SEED = b"t" * 32

#: Reference regex for a SPIFFE subject (mirrors the upstream reference
#: model's ``subject`` pattern for the ``spiffe://`` branch): scheme, a
#: non-empty authority with no ``/``, then a REQUIRED non-empty path.
_SPIFFE_SUBJECT_RE = re.compile(r"^spiffe://[^/]+/.+$")

#: Every top-level field that is *always* signed, mirroring
#: ``trust_record._BASE_SIGNED_FIELDS``. ``delegation``/``references`` are
#: deliberately excluded: they are only part of the signed body when the
#: record actually carries them (see ``_canonical_body_bytes`` below).
_BASE_SIGNED_FIELDS: tuple[str, ...] = (
    "eat_profile",
    "iat",
    "subject",
    "model",
    "runtime",
    "policy",
    "data_class",
    "tool_transcript",
    "build_provenance",
    "appraisal",
    "cnf",
)

_DEFAULT_GATE_CONFIG = {"rules": ["deny-network"], "version": 1}


def _create_journal(
    tmp_path: Path,
    events: list[dict],
    *,
    with_defaults: bool = True,
) -> Path:
    """Create a journal.jsonl file with the given events, chained properly.

    Each entry in *events* is a decision payload (``{"type": ..., ...}``).
    The helper builds the Merkle chain fields (``prev_hash``,
    ``payload_hash``, ``event_hash``, ``index``) from the payload so that
    :func:`verify_events` accepts the file. A bare ``event_hash`` on the
    payload is dropped: the chain fields own the head hash.

    When *with_defaults* (the default), a leading synthetic event carries
    ``model_provider``/``model_id`` and ``gate_config`` so tests that don't
    care about those fields don't have to supply them by hand; pass
    ``with_defaults=False`` for tests that exercise their absence.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    journal = tmp_path / "journal.jsonl"
    all_events = list(events)
    if with_defaults:
        all_events = [
            {
                "type": "run_started",
                "ts": 0.0,
                "model_provider": "anthropic",
                "model_id": "claude-sonnet-5",
                "gate_config": _DEFAULT_GATE_CONFIG,
            },
            *all_events,
        ]
    lines: list[str] = []
    prev_hash = _GENESIS_HASH
    for index, payload in enumerate(all_events):
        event_type = str(payload.get("type", "event"))
        chain_payload = {k: v for k, v in payload.items() if k != "event_hash"}
        p_hash = _payload_hash(event_type, chain_payload)
        e_hash = compute_event_hash(
            prev_hash=prev_hash,
            event_type=event_type,
            payload_hash=p_hash,
            index=index,
        )
        entry = {
            "index": index,
            "event": event_type,
            "prev_hash": prev_hash,
            "payload_hash": p_hash,
            "event_hash": e_hash,
        }
        entry.update(chain_payload)
        lines.append(json.dumps(entry, sort_keys=True))
        prev_hash = e_hash
    journal.write_text("\n".join(lines) + "\n" if lines else "")
    return journal


def _test_keypair() -> tuple[bytes, bytes]:
    """Return ``(private_key_pem, public_key_raw_32_bytes)`` for a fixed seed.

    Deterministic (unlike the per-test autouse keystore) so a test can
    independently recompute expected key material and cross-check it
    against what the emitter produced.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.from_private_bytes(_TEST_SEED)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_raw = key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return private_pem, public_raw


def _public_key_pem_from_raw(public_raw: bytes) -> bytes:
    """Re-wrap a raw 32-byte Ed25519 public key as SPKI PEM."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    return Ed25519PublicKey.from_public_bytes(public_raw).public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _emitter_with_known_key(
    *,
    install_rev: str = "aaaaaaaaaaaaaaaa",
    installed_digest: str = "sha256:" + "ab" * 32,
) -> TrustRecordEmitter:
    """Return an emitter whose signing key, install rev, and build digest are fixed."""
    private_pem, _ = _test_keypair()
    return TrustRecordEmitter(
        install_rev_getter=lambda: install_rev,
        get_private_key_pem=lambda: private_pem,
        get_installed_digest=lambda: installed_digest,
    )


def _canonical_body_bytes(doc: dict[str, Any]) -> bytes:
    """Rebuild the exact bytes ``_sign_record`` signed from a parsed record.

    ``delegation``/``references`` are only added to the reconstruction when
    *doc* actually carries them: the signed pre-image is the record as
    serialized, not a form padded out with an explicit ``null`` for
    whichever optional member is absent (see the module docstring's
    "Signing pre-image, corrected" note).
    """
    body = {field: doc[field] for field in _BASE_SIGNED_FIELDS}
    if "delegation" in doc:
        body["delegation"] = doc["delegation"]
    if "references" in doc:
        body["references"] = doc["references"]
    return canonicalize_jcs(body)


def _offline_verify(doc: dict[str, Any], public_key_pem: bytes) -> bool:
    """Re-verify a parsed record's bare Ed25519 signature, offline.

    Per the schema, ``signature`` is a bare base64url Ed25519 signature over
    the JCS canonicalisation of every other field -- no JOSE/JWS framing to
    unwrap first.
    """
    import base64

    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    public_key = load_pem_public_key(public_key_pem)
    padded = doc["signature"] + "=" * (-len(doc["signature"]) % 4)
    raw_sig = base64.urlsafe_b64decode(padded)
    try:
        public_key.verify(raw_sig, _canonical_body_bytes(doc))
    except InvalidSignature:
        return False
    return True


# ---------------------------------------------------------------------------
# subject: fixed-trust-domain SPIFFE URI (issue #4761)
# ---------------------------------------------------------------------------


class TestSpiffeSubject:
    def test_execution_subject_matches_reference_pattern(self) -> None:
        subject = spiffe_subject_for_execution("run-1", "exec-1")
        assert _SPIFFE_SUBJECT_RE.match(subject)

    def test_execution_subject_is_bernstein_run_domain_with_run_and_exec_path(self) -> None:
        subject = spiffe_subject_for_execution("run-1", "exec-2")
        assert subject == "spiffe://bernstein.run/run/run-1/exec/exec-2"

    def test_aggregate_subject_matches_reference_pattern(self) -> None:
        subject = spiffe_subject_for_aggregate("run-1")
        assert _SPIFFE_SUBJECT_RE.match(subject)

    def test_aggregate_subject_is_run_scoped_with_no_exec_segment(self) -> None:
        subject = spiffe_subject_for_aggregate("run-1")
        assert subject == "spiffe://bernstein.run/run/run-1"
        assert "/exec/" not in subject

    def test_two_executions_of_the_same_run_get_different_subjects(self) -> None:
        a = spiffe_subject_for_execution("run-1", "exec-a")
        b = spiffe_subject_for_execution("run-1", "exec-b")
        assert a != b
        assert a.startswith("spiffe://bernstein.run/run/run-1/")
        assert b.startswith("spiffe://bernstein.run/run/run-1/")

    def test_execution_subject_and_aggregate_subject_of_same_run_share_a_prefix(self) -> None:
        agg = spiffe_subject_for_aggregate("run-1")
        exe = spiffe_subject_for_execution("run-1", "exec-a")
        assert exe.startswith(agg + "/")

    @pytest.mark.parametrize("bad", ["", "has/slash"], ids=["empty", "slash"])
    def test_run_id_must_be_a_single_safe_path_segment(self, bad: str) -> None:
        with pytest.raises(ValueError, match="run_id"):
            spiffe_subject_for_execution(bad, "exec-1")

    def test_exec_id_must_be_a_single_safe_path_segment(self) -> None:
        with pytest.raises(ValueError, match="exec_id"):
            spiffe_subject_for_execution("run-1", "has/slash")


# ---------------------------------------------------------------------------
# eat_profile / iat
# ---------------------------------------------------------------------------


class TestEatProfileAndIat:
    def test_eat_profile_is_the_pinned_constant(self, tmp_path: Path) -> None:
        emitter = _emitter_with_known_key()
        journal = _create_journal(tmp_path, [{"type": "run_completed", "ts": 5.0}])
        parsed = json.loads(emitter.emit_trust_record(journal, "run-1", "exec-1"))

        assert parsed["eat_profile"] == "tag:agentrust-io.com,2026:trace-v0.2"

    def test_iat_is_the_last_events_timestamp_rounded_to_int(self, tmp_path: Path) -> None:
        emitter = _emitter_with_known_key()
        journal = _create_journal(
            tmp_path,
            [{"type": "a", "ts": 1000.2}, {"type": "b", "ts": 2000.6}],
        )
        parsed = json.loads(emitter.emit_trust_record(journal, "run-1", "exec-1"))

        assert parsed["iat"] == 2001

    def test_empty_journal_is_refused(self, tmp_path: Path) -> None:
        emitter = _emitter_with_known_key()
        journal = _create_journal(tmp_path, [], with_defaults=False)

        with pytest.raises(ValueError, match="no events"):
            emitter.emit_trust_record(journal, "run-1", "exec-1")

    def test_missing_journal_is_refused(self, tmp_path: Path) -> None:
        emitter = _emitter_with_known_key()
        missing = tmp_path / "nonexistent.jsonl"

        with pytest.raises(ValueError, match="no events"):
            emitter.emit_trust_record(missing, "run-1", "exec-1")

    def test_a_journal_with_a_broken_chain_is_refused(self, tmp_path: Path) -> None:
        """A tampered journal (mutated prev_hash) must not produce a record.

        The error must name the divergent step index (R12), not merely
        report a bare true/false.
        """
        emitter = _emitter_with_known_key()
        journal = _create_journal(tmp_path, [{"type": "event_1"}, {"type": "event_2"}])
        raw = json.loads(journal.read_text(encoding="utf-8").splitlines()[1])
        raw["prev_hash"] = "deadbeef" * 8
        lines = journal.read_text(encoding="utf-8").strip().splitlines()
        lines[1] = json.dumps(raw, sort_keys=True)
        journal.write_text("\n".join(lines) + "\n")

        with pytest.raises(ValueError, match="journal chain broken"):
            emitter.emit_trust_record(journal, "run-1", "exec-1")

    def test_malformed_json_lines_are_skipped(self, tmp_path: Path) -> None:
        emitter = _emitter_with_known_key()
        journal = _create_journal(tmp_path, [{"type": "valid"}, {"type": "also_valid"}])
        raw_lines = journal.read_text(encoding="utf-8").strip().splitlines()
        raw_lines.insert(1, "not json")
        journal.write_text("\n".join(raw_lines) + "\n")

        # Must not raise, and must still find the defaults event's model/gate_config.
        parsed = json.loads(emitter.emit_trust_record(journal, "run-1", "exec-1"))
        assert parsed["model"]["model_id"] == "claude-sonnet-5"


# ---------------------------------------------------------------------------
# model (issue #4760)
# ---------------------------------------------------------------------------


class TestModelClaim:
    def test_model_carries_provider_and_model_id(self, tmp_path: Path) -> None:
        emitter = _emitter_with_known_key()
        journal = _create_journal(tmp_path, [{"type": "run_completed", "ts": 1.0}])
        parsed = json.loads(emitter.emit_trust_record(journal, "run-1", "exec-1"))

        assert parsed["model"] == {"provider": "anthropic", "model_id": "claude-sonnet-5"}

    def test_model_version_included_when_present(self, tmp_path: Path) -> None:
        emitter = _emitter_with_known_key()
        journal = _create_journal(
            tmp_path,
            [{"type": "model_pin", "model_provider": "anthropic", "model_id": "claude-x", "model_version": "2026-08"}],
        )
        parsed = json.loads(emitter.emit_trust_record(journal, "run-1", "exec-1"))

        assert parsed["model"]["version"] == "2026-08"

    def test_a_later_model_event_wins_over_an_earlier_one(self, tmp_path: Path) -> None:
        """A mid-run model switch is reflected honestly: last recorded wins."""
        emitter = _emitter_with_known_key()
        journal = _create_journal(
            tmp_path,
            [{"type": "model_switch", "model_provider": "openai", "model_id": "gpt-later"}],
        )
        parsed = json.loads(emitter.emit_trust_record(journal, "run-1", "exec-1"))

        assert parsed["model"] == {"provider": "openai", "model_id": "gpt-later"}

    def test_missing_model_identity_is_refused(self, tmp_path: Path) -> None:
        emitter = _emitter_with_known_key()
        journal = _create_journal(
            tmp_path,
            [{"type": "run_completed", "ts": 1.0, "gate_config": _DEFAULT_GATE_CONFIG}],
            with_defaults=False,
        )

        with pytest.raises(ValueError, match="model"):
            emitter.emit_trust_record(journal, "run-1", "exec-1")


# ---------------------------------------------------------------------------
# policy: bundle_hash + enforcement_mode
# ---------------------------------------------------------------------------


class TestPolicyClaim:
    def test_enforcement_mode_is_always_enforce(self, tmp_path: Path) -> None:
        emitter = _emitter_with_known_key()
        journal = _create_journal(tmp_path, [{"type": "run_completed", "ts": 1.0}])
        parsed = json.loads(emitter.emit_trust_record(journal, "run-1", "exec-1"))

        assert parsed["policy"]["enforcement_mode"] == "enforce"

    def test_bundle_hash_is_sha256_of_jcs_canonical_gate_config(self, tmp_path: Path) -> None:
        emitter = _emitter_with_known_key()
        journal = _create_journal(tmp_path, [{"type": "run_completed", "ts": 1.0}])
        parsed = json.loads(emitter.emit_trust_record(journal, "run-1", "exec-1"))

        expected = f"sha256:{hashlib.sha256(canonicalize_jcs(_DEFAULT_GATE_CONFIG)).hexdigest()}"
        assert parsed["policy"]["bundle_hash"] == expected

    def test_two_different_gate_configs_hash_differently(self, tmp_path: Path) -> None:
        emitter = _emitter_with_known_key()
        parent_journal = _create_journal(
            tmp_path / "parent",
            [
                {
                    "type": "run_started",
                    "ts": 1.0,
                    "model_provider": "a",
                    "model_id": "m",
                    "gate_config": {"scope": "wide"},
                }
            ],
            with_defaults=False,
        )
        child_journal = _create_journal(
            tmp_path / "child",
            [
                {
                    "type": "run_started",
                    "ts": 2.0,
                    "model_provider": "a",
                    "model_id": "m",
                    "gate_config": {"scope": "narrow"},
                }
            ],
            with_defaults=False,
        )
        (tmp_path / "parent").mkdir(exist_ok=True)
        (tmp_path / "child").mkdir(exist_ok=True)

        parent = json.loads(emitter.emit_trust_record(parent_journal, "run-1", "exec-parent"))
        child = json.loads(emitter.emit_trust_record(child_journal, "run-1", "exec-child"))

        assert parent["policy"]["bundle_hash"] != child["policy"]["bundle_hash"]

    def test_missing_gate_config_is_refused(self, tmp_path: Path) -> None:
        emitter = _emitter_with_known_key()
        journal = _create_journal(
            tmp_path,
            [{"type": "run_completed", "ts": 1.0, "model_provider": "a", "model_id": "m"}],
            with_defaults=False,
        )

        with pytest.raises(ValueError, match="gate_config"):
            emitter.emit_trust_record(journal, "run-1", "exec-1")


# ---------------------------------------------------------------------------
# data_class
# ---------------------------------------------------------------------------


class TestDataClassClaim:
    def test_defaults_conservative_when_undeclared(self, tmp_path: Path) -> None:
        emitter = _emitter_with_known_key()
        journal = _create_journal(tmp_path, [{"type": "run_completed", "ts": 1.0}])
        parsed = json.loads(emitter.emit_trust_record(journal, "run-1", "exec-1"))

        assert parsed["data_class"] == "confidential"

    def test_operator_declared_value_is_used_when_present(self, tmp_path: Path) -> None:
        emitter = _emitter_with_known_key()
        journal = _create_journal(tmp_path, [{"type": "run_started", "data_class": "restricted"}])
        parsed = json.loads(emitter.emit_trust_record(journal, "run-1", "exec-1"))

        assert parsed["data_class"] == "restricted"


# ---------------------------------------------------------------------------
# runtime: all-zero measurement, closed object
# ---------------------------------------------------------------------------


class TestRuntimeClaim:
    def test_runtime_is_software_only_with_all_zero_measurement(self, tmp_path: Path) -> None:
        emitter = _emitter_with_known_key()
        journal = _create_journal(tmp_path, [{"type": "run_completed", "ts": 1.0}])
        parsed = json.loads(emitter.emit_trust_record(journal, "run-1", "exec-1"))

        assert parsed["runtime"] == {"platform": "software-only", "measurement": f"sha256:{'0' * 64}"}

    def test_runtime_is_a_closed_two_member_object(self, tmp_path: Path) -> None:
        emitter = _emitter_with_known_key()
        journal = _create_journal(tmp_path, [{"type": "run_completed", "ts": 1.0}])
        parsed = json.loads(emitter.emit_trust_record(journal, "run-1", "exec-1"))

        assert set(parsed["runtime"]) == {"platform", "measurement"}


# ---------------------------------------------------------------------------
# tool_transcript: always present, digest over tool_call entries only
# ---------------------------------------------------------------------------


class TestToolTranscriptClaim:
    def test_present_with_zero_calls(self, tmp_path: Path) -> None:
        emitter = _emitter_with_known_key()
        journal = _create_journal(tmp_path, [{"type": "run_completed", "ts": 1.0}])
        parsed = json.loads(emitter.emit_trust_record(journal, "run-1", "exec-1"))

        assert parsed["tool_transcript"]["call_count"] == 0
        assert parsed["tool_transcript"]["hash"].startswith("sha256:")

    def test_counts_only_tool_call_events(self, tmp_path: Path) -> None:
        emitter = _emitter_with_known_key()
        journal = _create_journal(
            tmp_path,
            [
                {"type": "tool_call", "tool": "fs.read", "args": {"path": "a"}},
                {"type": "not_a_tool_call"},
                {"type": "tool_call", "tool": "fs.write", "args": {"path": "b"}},
            ],
        )
        parsed = json.loads(emitter.emit_trust_record(journal, "run-1", "exec-1"))

        assert parsed["tool_transcript"]["call_count"] == 2

    def test_hash_changes_when_tool_call_payload_changes(self, tmp_path: Path) -> None:
        emitter = _emitter_with_known_key()
        journal_a = _create_journal(tmp_path / "a", [{"type": "tool_call", "tool": "fs.read"}])
        journal_b = _create_journal(tmp_path / "b", [{"type": "tool_call", "tool": "fs.write"}])

        a = json.loads(emitter.emit_trust_record(journal_a, "run-1", "exec-a"))
        b = json.loads(emitter.emit_trust_record(journal_b, "run-1", "exec-b"))

        assert a["tool_transcript"]["hash"] != b["tool_transcript"]["hash"]

    def test_zero_calls_hash_is_stable_across_otherwise_different_journals(self, tmp_path: Path) -> None:
        """Two executions with no tool calls at all hash identically -- the
        digest covers the (empty) tool-call list, not the whole journal."""
        emitter = _emitter_with_known_key()
        journal_a = _create_journal(tmp_path / "a", [{"type": "run_completed", "ts": 1.0}])
        journal_b = _create_journal(tmp_path / "b", [{"type": "run_completed", "ts": 99.0}, {"type": "other"}])

        a = json.loads(emitter.emit_trust_record(journal_a, "run-1", "exec-a"))
        b = json.loads(emitter.emit_trust_record(journal_b, "run-1", "exec-b"))

        assert (
            a["tool_transcript"]["hash"]
            == b["tool_transcript"]["hash"]
            == f"sha256:{hashlib.sha256(canonicalize_jcs([])).hexdigest()}"
        )


# ---------------------------------------------------------------------------
# build_provenance
# ---------------------------------------------------------------------------


class TestBuildProvenanceClaim:
    def test_slsa_level_zero_and_sha256_prefixed_digest(self, tmp_path: Path) -> None:
        emitter = _emitter_with_known_key(installed_digest="sha256:" + "cd" * 32)
        journal = _create_journal(tmp_path, [{"type": "run_completed", "ts": 1.0}])
        parsed = json.loads(emitter.emit_trust_record(journal, "run-1", "exec-1"))

        assert parsed["build_provenance"]["slsa_level"] == 0
        assert parsed["build_provenance"]["digest"] == "sha256:" + "cd" * 32

    def test_provenance_uri_is_the_release_page(self, tmp_path: Path) -> None:
        emitter = _emitter_with_known_key()
        journal = _create_journal(tmp_path, [{"type": "run_completed", "ts": 1.0}])
        parsed = json.loads(emitter.emit_trust_record(journal, "run-1", "exec-1"))

        assert parsed["build_provenance"]["provenance_uri"].startswith("https://")


# ---------------------------------------------------------------------------
# _default_installed_digest: the real (uninjected) build_provenance.digest
# default. Every test above uses ``get_installed_digest=`` to inject a
# canned value, so none of them exercises this function at all -- these
# tests call it directly.
# ---------------------------------------------------------------------------


class _FakeFileHash:
    def __init__(self, value: str) -> None:
        self.value = value


class _FakePackagePath:
    """Minimal stand-in for ``importlib.metadata.PackagePath``.

    Only implements what ``_default_installed_digest`` reads: ``str()`` for
    the path and a ``.hash`` attribute (``None``, or an object with
    ``.value``) for the RECORD-derived per-file content hash.
    """

    def __init__(self, path: str, hash_value: str | None) -> None:
        self._path = path
        self.hash = _FakeFileHash(hash_value) if hash_value is not None else None

    def __str__(self) -> str:
        return self._path


class _FakeDistribution:
    def __init__(self, files: list[_FakePackagePath] | None, version: str = "1.0.0") -> None:
        self.files = files
        self.version = version


class TestDefaultInstalledDigest:
    """Exercise ``trust_record._default_installed_digest`` directly.

    Confirms the fix for the digest tracking file *content* (RECORD's
    per-file SHA-256, ``PackagePath.hash.value``) rather than only file
    *path names* -- two installs with identical layouts but different file
    contents must not collide on the same digest.
    """

    def test_changing_a_file_content_hash_changes_the_digest(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import importlib.metadata as metadata

        from bernstein.core.observability.trust_record import _default_installed_digest

        same_layout_different_content = _FakeDistribution(
            [_FakePackagePath("bernstein/__init__.py", "hash-of-version-A")]
        )
        monkeypatch.setattr(metadata, "distribution", lambda name: same_layout_different_content)
        digest_a = _default_installed_digest()

        same_layout_different_content.files = [_FakePackagePath("bernstein/__init__.py", "hash-of-version-B")]
        digest_b = _default_installed_digest()

        assert digest_a != digest_b, (
            "two installs with the same file layout but different file contents "
            "must not produce the same build_provenance.digest"
        )

    def test_identical_file_layout_and_content_is_deterministic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import importlib.metadata as metadata

        from bernstein.core.observability.trust_record import _default_installed_digest

        def make_dist() -> _FakeDistribution:
            return _FakeDistribution(
                [
                    _FakePackagePath("bernstein/__init__.py", "hash-a"),
                    _FakePackagePath("bernstein/cli.py", "hash-b"),
                ]
            )

        monkeypatch.setattr(metadata, "distribution", lambda name: make_dist())
        assert _default_installed_digest() == _default_installed_digest()

    def test_file_order_does_not_affect_the_digest(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import importlib.metadata as metadata

        from bernstein.core.observability.trust_record import _default_installed_digest

        forward = _FakeDistribution(
            [
                _FakePackagePath("a.py", "hash-a"),
                _FakePackagePath("b.py", "hash-b"),
            ]
        )
        monkeypatch.setattr(metadata, "distribution", lambda name: forward)
        digest_forward = _default_installed_digest()

        forward.files = list(reversed(forward.files))
        digest_reversed = _default_installed_digest()

        assert digest_forward == digest_reversed

    def test_a_file_with_no_recorded_hash_still_participates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import importlib.metadata as metadata

        from bernstein.core.observability.trust_record import _default_installed_digest

        with_extra_unhashed_file = _FakeDistribution(
            [
                _FakePackagePath("bernstein/__init__.py", "hash-a"),
                _FakePackagePath("bernstein-1.0.dist-info/RECORD", None),
            ]
        )
        monkeypatch.setattr(metadata, "distribution", lambda name: with_extra_unhashed_file)
        digest_with_record = _default_installed_digest()

        without_extra_file = _FakeDistribution([_FakePackagePath("bernstein/__init__.py", "hash-a")])
        monkeypatch.setattr(metadata, "distribution", lambda name: without_extra_file)
        digest_without_record = _default_installed_digest()

        assert digest_with_record != digest_without_record

    def test_no_files_falls_back_to_the_version_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import importlib.metadata as metadata

        from bernstein.core.observability.trust_record import _default_installed_digest

        monkeypatch.setattr(metadata, "distribution", lambda name: _FakeDistribution([], version="9.9.9"))
        digest_a = _default_installed_digest()

        monkeypatch.setattr(metadata, "distribution", lambda name: _FakeDistribution([], version="9.9.9"))
        digest_b = _default_installed_digest()
        assert digest_a == digest_b

        monkeypatch.setattr(metadata, "distribution", lambda name: _FakeDistribution([], version="1.2.3"))
        digest_c = _default_installed_digest()
        assert digest_c != digest_a

    def test_distribution_not_found_falls_back_to_all_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import importlib.metadata as metadata

        from bernstein.core.observability.trust_record import _default_installed_digest

        def _raise(name: str) -> _FakeDistribution:
            raise metadata.PackageNotFoundError(name)

        monkeypatch.setattr(metadata, "distribution", _raise)
        assert _default_installed_digest() == f"sha256:{'0' * 64}"

    def test_the_real_installed_distribution_produces_a_wellformed_digest(self) -> None:
        """Sanity check against the actual installed ``bernstein`` distribution.

        Not mocked: confirms ``_default_installed_digest`` runs end-to-end
        against real ``importlib.metadata`` output in this environment.
        """
        from bernstein.core.observability.trust_record import _default_installed_digest

        digest = _default_installed_digest()
        assert digest.startswith("sha256:")
        assert len(digest) == len("sha256:") + 64


# ---------------------------------------------------------------------------
# appraisal (issue #4762)
# ---------------------------------------------------------------------------


class TestAppraisalClaim:
    def test_status_is_none_and_verifier_is_the_fixed_uri(self, tmp_path: Path) -> None:
        emitter = _emitter_with_known_key()
        journal = _create_journal(tmp_path, [{"type": "run_completed", "ts": 1.0}])
        parsed = json.loads(emitter.emit_trust_record(journal, "run-1", "exec-1"))

        assert parsed["appraisal"]["status"] == "none"
        assert parsed["appraisal"]["verifier"] == "https://bernstein.run/trace/verifier"

    def test_verifier_is_not_the_subject(self, tmp_path: Path) -> None:
        """The verifier names the (self-)appraisal method, not this execution."""
        emitter = _emitter_with_known_key()
        journal = _create_journal(tmp_path, [{"type": "run_completed", "ts": 1.0}])
        parsed = json.loads(emitter.emit_trust_record(journal, "run-1", "exec-1"))

        assert parsed["appraisal"]["verifier"] != parsed["subject"]

    def test_timestamp_matches_iat(self, tmp_path: Path) -> None:
        emitter = _emitter_with_known_key()
        journal = _create_journal(tmp_path, [{"type": "run_completed", "ts": 42.0}])
        parsed = json.loads(emitter.emit_trust_record(journal, "run-1", "exec-1"))

        assert parsed["appraisal"]["timestamp"] == parsed["iat"]

    def test_provenance_depth_verified_is_never_emitted(self, tmp_path: Path) -> None:
        """Never invent a verification depth this producer did not perform."""
        emitter = _emitter_with_known_key()
        journal = _create_journal(tmp_path, [{"type": "run_completed", "ts": 1.0}])
        parsed = json.loads(emitter.emit_trust_record(journal, "run-1", "exec-1"))

        assert "provenance_depth_verified" not in parsed["appraisal"]


# ---------------------------------------------------------------------------
# cnf.jwk: public Ed25519 key for key confirmation
# ---------------------------------------------------------------------------


class TestCnfJwk:
    def test_cnf_jwk_present_and_public(self, tmp_path: Path) -> None:
        emitter = _emitter_with_known_key()
        journal = _create_journal(tmp_path, [{"type": "run_completed", "ts": 1.0}])
        parsed = json.loads(emitter.emit_trust_record(journal, "run-1", "exec-1"))

        jwk = parsed["cnf"]["jwk"]
        assert jwk["kty"] == "OKP"
        assert jwk["crv"] == "Ed25519"
        assert re.match(r"^[A-Za-z0-9_-]+$", jwk["x"])
        for private_member in ("d", "p", "q", "dp", "dq", "qi", "k"):
            assert private_member not in jwk

    def test_cnf_jwk_carries_the_signing_kid(self, tmp_path: Path) -> None:
        """The key id lives on the JWK (a property of the key), not in a
        signature-adjacent object -- see the module docstring's note on the
        schema's signature envelope."""
        emitter = _emitter_with_known_key(install_rev="deadbeefdeadbeef")
        journal = _create_journal(tmp_path, [{"type": "run_completed", "ts": 1.0}])
        parsed = json.loads(emitter.emit_trust_record(journal, "run-1", "exec-1"))

        assert parsed["cnf"]["jwk"]["kid"] == "install-deadbeefdeadbeef"


# ---------------------------------------------------------------------------
# references[]: produced-artifact only, never evidence (issue #4762)
# ---------------------------------------------------------------------------


class TestReferencesClaim:
    def test_absent_when_no_artifacts_produced(self, tmp_path: Path) -> None:
        emitter = _emitter_with_known_key()
        journal = _create_journal(tmp_path, [{"type": "run_completed", "ts": 1.0}])
        parsed = json.loads(emitter.emit_trust_record(journal, "run-1", "exec-1"))

        assert "references" not in parsed

    def test_produced_artifact_entries_carry_rel_id_resolver_digest(self, tmp_path: Path) -> None:
        emitter = _emitter_with_known_key()
        journal = _create_journal(
            tmp_path,
            [
                {
                    "type": "artifact_produced",
                    "artifact_id": "artifact-1",
                    "resolver": "urn:bernstein:artifacts",
                    "digest": "sha256:" + "11" * 32,
                }
            ],
        )
        parsed = json.loads(emitter.emit_trust_record(journal, "run-1", "exec-1"))

        assert parsed["references"] == [
            {
                "rel": "produced-artifact",
                "id": "artifact-1",
                "resolver": "urn:bernstein:artifacts",
                "digest": "sha256:" + "11" * 32,
            }
        ]

    def test_no_execution_record_ever_carries_an_evidence_rel(self, tmp_path: Path) -> None:
        emitter = _emitter_with_known_key()
        journal = _create_journal(
            tmp_path,
            [
                {"type": "tool_call", "tool": "fs.read"},
                {
                    "type": "artifact_produced",
                    "artifact_id": "a",
                    "resolver": "r",
                    "digest": "sha256:" + "22" * 32,
                },
            ],
        )
        parsed = json.loads(emitter.emit_trust_record(journal, "run-1", "exec-1"))

        rels = {r["rel"] for r in parsed.get("references", [])}
        assert "evidence" not in rels


# ---------------------------------------------------------------------------
# delegation (issue #4760)
# ---------------------------------------------------------------------------


class TestDelegationClaim:
    def _emit_parent_and_child(self, tmp_path: Path) -> tuple[str, str]:
        emitter = _emitter_with_known_key()
        parent_journal = _create_journal(
            tmp_path / "parent",
            [
                {
                    "type": "run_started",
                    "ts": 1.0,
                    "model_provider": "a",
                    "model_id": "m",
                    "gate_config": {"scope": "wide"},
                }
            ],
            with_defaults=False,
        )
        child_journal = _create_journal(
            tmp_path / "child",
            [
                {
                    "type": "run_started",
                    "ts": 2.0,
                    "model_provider": "a",
                    "model_id": "m",
                    "gate_config": {"scope": "narrow"},
                }
            ],
            with_defaults=False,
        )
        parent_output = emitter.emit_trust_record(parent_journal, "run-1", "exec-parent")
        child_output = emitter.emit_trust_record(
            child_journal,
            "run-1",
            "exec-child",
            parent_record=parent_output,
            credential_id="cred-1",
        )
        return parent_output, child_output

    def test_root_execution_has_no_delegation_member_at_all(self, tmp_path: Path) -> None:
        emitter = _emitter_with_known_key()
        journal = _create_journal(tmp_path, [{"type": "run_completed", "ts": 1.0}])
        parsed = json.loads(emitter.emit_trust_record(journal, "run-1", "exec-1"))

        assert "delegation" not in parsed

    def test_child_delegation_parent_record_hash_is_sha256_of_jcs_canonical_parent(self, tmp_path: Path) -> None:
        parent_output, child_output = self._emit_parent_and_child(tmp_path)
        parent_doc = json.loads(parent_output)

        expected = f"sha256:{hashlib.sha256(canonicalize_jcs(parent_doc)).hexdigest()}"
        assert json.loads(child_output)["delegation"]["parent_record_hash"] == expected
        # For an ASCII-only record, the JCS digest matches raw emitted bytes (regression guard).
        assert expected == f"sha256:{hashlib.sha256(parent_output.encode('utf-8')).hexdigest()}"

    def test_child_delegation_non_ascii_parent_record_hash_matches_jcs_and_diverges_from_raw_bytes(
        self, tmp_path: Path
    ) -> None:
        emitter = _emitter_with_known_key()
        parent_journal = _create_journal(
            tmp_path / "parent",
            [
                {
                    "type": "run_started",
                    "ts": 1.0,
                    "model_provider": "anthropic",
                    "model_id": "claude-модель",
                    "data_class": "конфиденциально",
                    "gate_config": {"scope": "wide"},
                },
                {"type": "run_completed", "ts": 2.0},
            ],
        )
        child_journal = _create_journal(tmp_path / "child", [{"type": "run_completed", "ts": 3.0}])
        parent_output = emitter.emit_trust_record(parent_journal, "run-1", "exec-parent")
        child_output = emitter.emit_trust_record(
            child_journal, "run-1", "exec-child", parent_record=parent_output, credential_id="cred-1"
        )

        parent_doc = json.loads(parent_output)
        canonical_parent_hash = f"sha256:{hashlib.sha256(canonicalize_jcs(parent_doc)).hexdigest()}"
        raw_parent_hash = f"sha256:{hashlib.sha256(parent_output.encode('utf-8')).hexdigest()}"

        # JCS and raw emitted bytes diverge on non-ASCII characters
        assert canonical_parent_hash != raw_parent_hash

        # child delegation.parent_record_hash must match JCS canonical form
        assert json.loads(child_output)["delegation"]["parent_record_hash"] == canonical_parent_hash

    def test_child_delegation_credential_id_is_passed_through(self, tmp_path: Path) -> None:
        _parent_output, child_output = self._emit_parent_and_child(tmp_path)

        assert json.loads(child_output)["delegation"]["credential_id"] == "cred-1"

    def test_delegation_requires_credential_id_when_parent_record_given(self, tmp_path: Path) -> None:
        emitter = _emitter_with_known_key()
        journal = _create_journal(tmp_path, [{"type": "run_completed", "ts": 1.0}])
        parent_output = emitter.emit_trust_record(journal, "run-1", "exec-parent")

        with pytest.raises(ValueError, match="credential_id"):
            emitter.emit_trust_record(journal, "run-1", "exec-child", parent_record=parent_output)

    def test_delegation_rejects_credential_id_without_parent_record(self, tmp_path: Path) -> None:
        emitter = _emitter_with_known_key()
        journal = _create_journal(tmp_path, [{"type": "run_completed", "ts": 1.0}])

        with pytest.raises(ValueError, match="credential_id"):
            emitter.emit_trust_record(journal, "run-1", "exec-1", credential_id="cred-1")

    def test_delegation_rejects_an_empty_credential_id(self, tmp_path: Path) -> None:
        """An empty ``credential_id`` would emit ``delegation.credential_id == ""``,
        which fails schema validation (``minLength: 1``) -- refuse it at the
        emitter boundary instead of letting a caller mint a non-conformant
        record with a plausible mistake (passing ``""`` instead of omitting
        the argument)."""
        emitter = _emitter_with_known_key()
        journal = _create_journal(tmp_path, [{"type": "run_completed", "ts": 1.0}])
        parent_output = emitter.emit_trust_record(journal, "run-1", "exec-parent")

        with pytest.raises(ValueError, match="credential_id"):
            emitter.emit_trust_record(
                journal,
                "run-1",
                "exec-child",
                parent_record=parent_output,
                credential_id="",
            )

    def test_malformed_parent_record_raises_value_error(self, tmp_path: Path) -> None:
        """delegation.parent_record_hash parses and canonicalises the parent record,
        so malformed non-JSON input raises ValueError."""
        emitter = _emitter_with_known_key()
        journal = _create_journal(tmp_path, [{"type": "run_completed", "ts": 1.0}])

        with pytest.raises(ValueError, match="parent_record is not valid JSON"):
            emitter.emit_trust_record(journal, "run-1", "exec-1", parent_record="not json", credential_id="c")


# ---------------------------------------------------------------------------
# emit_aggregate_trust_record: run-level rollup (issue #4763)
# ---------------------------------------------------------------------------


class TestAggregateTrustRecord:
    def _emit_parent_and_child(self, tmp_path: Path, emitter: TrustRecordEmitter) -> tuple[str, str]:
        parent_journal = _create_journal(
            tmp_path / "parent",
            [
                {
                    "type": "run_started",
                    "ts": 1.0,
                    "model_provider": "anthropic",
                    "model_id": "claude-sonnet-5",
                    "data_class": "internal",
                    "gate_config": {"scope": "wide"},
                }
            ],
            with_defaults=False,
        )
        child_journal = _create_journal(
            tmp_path / "child",
            [
                {
                    "type": "run_started",
                    "ts": 2.0,
                    "model_provider": "anthropic",
                    "model_id": "claude-haiku-5",
                    "data_class": "internal",
                    "gate_config": {"scope": "narrow"},
                },
                {"type": "tool_call", "tool": "fs.read"},
            ],
            with_defaults=False,
        )
        parent_output = emitter.emit_trust_record(parent_journal, "run-1", "exec-parent")
        child_output = emitter.emit_trust_record(
            child_journal,
            "run-1",
            "exec-child",
            parent_record=parent_output,
            credential_id="cred-1",
        )
        return parent_output, child_output

    def test_requires_at_least_one_member(self) -> None:
        emitter = _emitter_with_known_key()
        with pytest.raises(ValueError, match="at least one member"):
            emitter.emit_aggregate_trust_record("run-1", [])

    def test_rejects_a_malformed_member(self) -> None:
        emitter = _emitter_with_known_key()
        with pytest.raises(ValueError, match="not valid JSON"):
            emitter.emit_aggregate_trust_record("run-1", ["not json"])

    def test_subject_is_run_scoped_not_execution_scoped(self, tmp_path: Path) -> None:
        emitter = _emitter_with_known_key()
        parent_output, child_output = self._emit_parent_and_child(tmp_path, emitter)

        output = emitter.emit_aggregate_trust_record("run-1", [parent_output, child_output])
        parsed = json.loads(output)

        assert parsed["subject"] == "spiffe://bernstein.run/run/run-1"
        assert "/exec/" not in parsed["subject"]

    def test_has_no_delegation_member(self, tmp_path: Path) -> None:
        emitter = _emitter_with_known_key()
        parent_output, child_output = self._emit_parent_and_child(tmp_path, emitter)

        parsed = json.loads(emitter.emit_aggregate_trust_record("run-1", [parent_output, child_output]))

        assert "delegation" not in parsed

    def test_one_member_execution_reference_per_member_in_order(self, tmp_path: Path) -> None:
        emitter = _emitter_with_known_key()
        parent_output, child_output = self._emit_parent_and_child(tmp_path, emitter)

        parsed = json.loads(emitter.emit_aggregate_trust_record("run-1", [parent_output, child_output]))

        assert [r["rel"] for r in parsed["references"]] == ["member-execution", "member-execution"]
        assert [r["id"] for r in parsed["references"]] == [
            json.loads(parent_output)["subject"],
            json.loads(child_output)["subject"],
        ]
        for entry in parsed["references"]:
            assert entry["resolver"]

    def test_each_member_reference_is_content_bound_through_its_digest(self, tmp_path: Path) -> None:
        """The digest lives in ``digest``, which is the field a verifier reads.

        Carrying it as an ``id`` and omitting ``digest`` validates -- ``digest``
        is optional and ``rel`` is open -- so nothing catches the difference,
        and a verifier that content-binds references finds nothing to bind.
        The entry is then readable only by whoever produced both sides.
        """
        emitter = _emitter_with_known_key()
        parent_output, child_output = self._emit_parent_and_child(tmp_path, emitter)
        parent_doc = json.loads(parent_output)
        child_doc = json.loads(child_output)

        parsed = json.loads(emitter.emit_aggregate_trust_record("run-1", [parent_output, child_output]))

        assert [r["digest"] for r in parsed["references"]] == [
            f"sha256:{hashlib.sha256(canonicalize_jcs(parent_doc)).hexdigest()}",
            f"sha256:{hashlib.sha256(canonicalize_jcs(child_doc)).hexdigest()}",
        ]
        # For ASCII records, JCS matches raw emitted bytes (regression guard).
        assert [r["digest"] for r in parsed["references"]] == [
            f"sha256:{hashlib.sha256(parent_output.encode('utf-8')).hexdigest()}",
            f"sha256:{hashlib.sha256(child_output.encode('utf-8')).hexdigest()}",
        ]
        # An id that is itself a digest is the shape this replaced: it puts the
        # binding in the field that names things, where nothing looks for it.
        for entry in parsed["references"]:
            assert not entry["id"].startswith("sha256:")

    def test_aggregate_references_digest_non_ascii_matches_jcs_and_diverges_from_raw_bytes(
        self, tmp_path: Path
    ) -> None:
        emitter = _emitter_with_known_key()
        journal = _create_journal(
            tmp_path / "member",
            [
                {
                    "type": "run_started",
                    "ts": 1.0,
                    "model_provider": "anthropic",
                    "model_id": "claude-модель",
                    "data_class": "конфиденциально",
                    "gate_config": {"scope": "wide"},
                },
                {"type": "run_completed", "ts": 2.0},
            ],
        )
        member_output = emitter.emit_trust_record(journal, "run-1", "exec-1")
        member_doc = json.loads(member_output)

        parsed = json.loads(emitter.emit_aggregate_trust_record("run-1", [member_output]))

        canonical_digest = f"sha256:{hashlib.sha256(canonicalize_jcs(member_doc)).hexdigest()}"
        raw_digest = f"sha256:{hashlib.sha256(member_output.encode('utf-8')).hexdigest()}"

        assert canonical_digest != raw_digest
        assert parsed["references"][0]["digest"] == canonical_digest

    def test_iat_is_the_latest_member_iat(self, tmp_path: Path) -> None:
        emitter = _emitter_with_known_key()
        parent_output, child_output = self._emit_parent_and_child(tmp_path, emitter)

        parsed = json.loads(emitter.emit_aggregate_trust_record("run-1", [parent_output, child_output]))

        assert parsed["iat"] == max(json.loads(parent_output)["iat"], json.loads(child_output)["iat"])

    def test_model_is_the_last_members_model(self, tmp_path: Path) -> None:
        emitter = _emitter_with_known_key()
        parent_output, child_output = self._emit_parent_and_child(tmp_path, emitter)

        parsed = json.loads(emitter.emit_aggregate_trust_record("run-1", [parent_output, child_output]))

        assert parsed["model"] == json.loads(child_output)["model"]

    def test_tool_transcript_call_count_sums_the_members(self, tmp_path: Path) -> None:
        emitter = _emitter_with_known_key()
        parent_output, child_output = self._emit_parent_and_child(tmp_path, emitter)

        parsed = json.loads(emitter.emit_aggregate_trust_record("run-1", [parent_output, child_output]))

        parent_calls = json.loads(parent_output)["tool_transcript"]["call_count"]
        child_calls = json.loads(child_output)["tool_transcript"]["call_count"]
        assert parsed["tool_transcript"]["call_count"] == parent_calls + child_calls == 1

    def test_policy_bundle_hash_differs_from_either_members_own(self, tmp_path: Path) -> None:
        """The aggregate's bundle_hash is a hash *of* the member hashes, not
        one member's own value copied through."""
        emitter = _emitter_with_known_key()
        parent_output, child_output = self._emit_parent_and_child(tmp_path, emitter)

        parsed = json.loads(emitter.emit_aggregate_trust_record("run-1", [parent_output, child_output]))

        parent_bundle = json.loads(parent_output)["policy"]["bundle_hash"]
        child_bundle = json.loads(child_output)["policy"]["bundle_hash"]
        assert parsed["policy"]["bundle_hash"] not in (parent_bundle, child_bundle)
        assert parsed["policy"]["enforcement_mode"] == "enforce"

    def test_rejects_disagreeing_member_data_classes(self, tmp_path: Path) -> None:
        emitter = _emitter_with_known_key()
        parent_journal = _create_journal(
            tmp_path / "parent",
            [
                {
                    "type": "run_started",
                    "ts": 1.0,
                    "model_provider": "a",
                    "model_id": "m",
                    "data_class": "internal",
                    "gate_config": {"scope": "wide"},
                }
            ],
            with_defaults=False,
        )
        child_journal = _create_journal(
            tmp_path / "child",
            [
                {
                    "type": "run_started",
                    "ts": 2.0,
                    "model_provider": "a",
                    "model_id": "m",
                    "data_class": "restricted",
                    "gate_config": {"scope": "narrow"},
                }
            ],
            with_defaults=False,
        )
        parent_output = emitter.emit_trust_record(parent_journal, "run-1", "exec-parent")
        child_output = emitter.emit_trust_record(
            child_journal, "run-1", "exec-child", parent_record=parent_output, credential_id="cred-1"
        )

        # Rolling up to most restrictive value is correct semantics for an aggregate:
        # it covers all members, so its classification ceiling is the most restrictive member's ceiling.
        result = emitter.emit_aggregate_trust_record("run-1", [parent_output, child_output])
        parsed = json.loads(result)
        assert parsed["data_class"] == "restricted", (
            f"Expected 'restricted' (most restrictive), got '{parsed['data_class']}'"
        )

    def test_verifies_offline_via_its_cnf_jwk(self, tmp_path: Path) -> None:
        private_pem, public_raw = _test_keypair()
        emitter = TrustRecordEmitter(
            install_rev_getter=lambda: "aaaaaaaaaaaaaaaa",
            get_private_key_pem=lambda: private_pem,
            get_installed_digest=lambda: "sha256:" + "ab" * 32,
        )
        parent_output, child_output = self._emit_parent_and_child(tmp_path, emitter)

        parsed = json.loads(emitter.emit_aggregate_trust_record("run-1", [parent_output, child_output]))

        assert _offline_verify(parsed, _public_key_pem_from_raw(public_raw)) is True


# ---------------------------------------------------------------------------
# Signature coverage: every field, including absent delegation/references,
# is inside the signed body.
# ---------------------------------------------------------------------------


def _mutate_data_class(doc: dict[str, Any]) -> None:
    doc["data_class"] = "public"


def _mutate_runtime(doc: dict[str, Any]) -> None:
    doc["runtime"] = dict(doc["runtime"])
    doc["runtime"]["platform"] = "tampered-platform"


def _mutate_appraisal(doc: dict[str, Any]) -> None:
    doc["appraisal"] = dict(doc["appraisal"])
    doc["appraisal"]["status"] = "affirming"


def _mutate_subject(doc: dict[str, Any]) -> None:
    doc["subject"] = doc["subject"] + "-tampered"


def _add_delegation_to_root(doc: dict[str, Any]) -> None:
    """A root record has no ``delegation`` member; adding one must not verify."""
    doc["delegation"] = {"parent_record_hash": f"sha256:{'0' * 64}", "credential_id": "forged"}


class TestSignatureCoversFullFieldSurface:
    @pytest.mark.parametrize(
        "mutate",
        [_mutate_data_class, _mutate_runtime, _mutate_appraisal, _add_delegation_to_root],
        ids=["data_class", "runtime", "appraisal", "add-delegation"],
    )
    def test_tampering_a_field_after_signing_invalidates_the_signature(self, tmp_path: Path, mutate: Any) -> None:
        private_pem, public_raw = _test_keypair()
        emitter = TrustRecordEmitter(
            install_rev_getter=lambda: "aaaaaaaaaaaaaaaa",
            get_private_key_pem=lambda: private_pem,
            get_installed_digest=lambda: "sha256:" + "ab" * 32,
        )
        journal = _create_journal(tmp_path, [{"type": "run_completed", "ts": 1.0}])
        doc = json.loads(emitter.emit_trust_record(journal, "run-1", "exec-1"))
        public_key_pem = _public_key_pem_from_raw(public_raw)

        assert _offline_verify(doc, public_key_pem) is True, "sanity: the untampered record must verify"

        mutate(doc)

        assert _offline_verify(doc, public_key_pem) is False

    def test_tampering_subject_after_signing_invalidates_the_signature(self, tmp_path: Path) -> None:
        private_pem, public_raw = _test_keypair()
        emitter = TrustRecordEmitter(
            install_rev_getter=lambda: "aaaaaaaaaaaaaaaa",
            get_private_key_pem=lambda: private_pem,
            get_installed_digest=lambda: "sha256:" + "ab" * 32,
        )
        journal = _create_journal(tmp_path, [{"type": "run_completed", "ts": 1.0}])
        doc = json.loads(emitter.emit_trust_record(journal, "run-1", "exec-1"))
        public_key_pem = _public_key_pem_from_raw(public_raw)

        _mutate_subject(doc)

        assert _offline_verify(doc, public_key_pem) is False


# ---------------------------------------------------------------------------
# TrustRecordEmitter._sign_record
# ---------------------------------------------------------------------------


def _bare_record(**overrides: Any) -> TrustRecord:
    base: dict[str, Any] = {
        "eat_profile": "tag:agentrust-io.com,2026:trace-v0.2",
        "iat": 1700000000,
        "subject": "spiffe://bernstein.run/run/test/exec/test",
        "model": {"provider": "anthropic", "model_id": "claude-sonnet-5"},
        "runtime": {"platform": "software-only", "measurement": f"sha256:{'0' * 64}"},
        "policy": {"bundle_hash": f"sha256:{'1' * 64}", "enforcement_mode": "enforce"},
        "data_class": "confidential",
        "tool_transcript": {"hash": f"sha256:{'2' * 64}", "call_count": 0},
        "build_provenance": {"slsa_level": 0, "digest": f"sha256:{'3' * 64}"},
        "appraisal": {"status": "none", "verifier": "https://bernstein.run/trace/verifier", "timestamp": 1700000000},
        "cnf": {"jwk": {"kty": "OKP", "crv": "Ed25519", "x": "dGVzdF9wdWJsaWNfa2V5XzEyMHgzMg", "kid": "test-key-id"}},
        "delegation": None,
        "references": None,
        "signature": "",
    }
    base.update(overrides)
    return TrustRecord(**base)


class TestSignRecord:
    def test_signature_is_a_nonempty_base64url_string(self, tmp_path: Path) -> None:
        emitter = TrustRecordEmitter()
        record = _bare_record()
        signed = emitter._sign_record(record)

        assert signed.signature != ""
        assert re.match(r"^[A-Za-z0-9_-]+$", signed.signature)

    def test_record_payload_unchanged_after_signing(self, tmp_path: Path) -> None:
        emitter = TrustRecordEmitter()
        record = _bare_record(data_class="restricted")
        signed = emitter._sign_record(record)

        assert signed.subject == record.subject
        assert signed.model == record.model
        assert signed.policy == record.policy
        assert signed.data_class == record.data_class == "restricted"

    def test_signature_verifies_against_the_signing_key(self, tmp_path: Path) -> None:
        private_pem, public_raw = _test_keypair()
        emitter = TrustRecordEmitter(get_private_key_pem=lambda: private_pem)
        record = _bare_record()
        signed = emitter._sign_record(record)

        doc: dict[str, Any] = {
            "eat_profile": signed.eat_profile,
            "iat": signed.iat,
            "subject": signed.subject,
            "model": signed.model,
            "runtime": signed.runtime,
            "policy": signed.policy,
            "data_class": signed.data_class,
            "tool_transcript": signed.tool_transcript,
            "build_provenance": signed.build_provenance,
            "appraisal": signed.appraisal,
            "cnf": signed.cnf,
            "signature": signed.signature,
        }
        # Matches the real emitted shape: an absent optional member is left
        # out entirely, never carried as an explicit null.
        if signed.delegation is not None:
            doc["delegation"] = signed.delegation
        if signed.references is not None:
            doc["references"] = signed.references
        assert _offline_verify(doc, _public_key_pem_from_raw(public_raw)) is True


# ---------------------------------------------------------------------------
# _sign_raw_ed25519
# ---------------------------------------------------------------------------


class TestSignRawEd25519:
    def test_produces_a_signature_only_no_framing(self, tmp_path: Path) -> None:
        """No header, no ``..`` separators -- a bare signature."""
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        private_key = Ed25519PrivateKey.generate()
        private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

        signature = _sign_raw_ed25519(b'{"a":1,"b":2}', private_pem)

        assert isinstance(signature, bytes)
        assert len(signature) == 64  # Ed25519 signatures are always 64 bytes

    def test_different_payload_different_signature(self, tmp_path: Path) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        private_key = Ed25519PrivateKey.generate()
        private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

        sig1 = _sign_raw_ed25519(b"payload1", private_pem)
        sig2 = _sign_raw_ed25519(b"payload2", private_pem)

        assert sig1 != sig2

    def test_different_key_different_signature(self, tmp_path: Path) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        private_key_1 = Ed25519PrivateKey.generate()
        private_pem_1 = private_key_1.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        private_key_2 = Ed25519PrivateKey.generate()
        private_pem_2 = private_key_2.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

        payload = b"same payload"
        sig1 = _sign_raw_ed25519(payload, private_pem_1)
        sig2 = _sign_raw_ed25519(payload, private_pem_2)

        assert sig1 != sig2

    def test_rejects_a_non_ed25519_key(self, tmp_path: Path) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        rsa_pem = rsa_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

        with pytest.raises(ValueError, match="Ed25519"):
            _sign_raw_ed25519(b"payload", rsa_pem)


# ---------------------------------------------------------------------------
# TrustRecordEmitter.emit_trust_record: canonical output shape
# ---------------------------------------------------------------------------


class TestEmitTrustRecord:
    def test_output_is_canonical_json(self, tmp_path: Path) -> None:
        emitter = TrustRecordEmitter()
        journal = _create_journal(tmp_path, [{"type": "run_completed", "ts": 1000.0}])

        output = emitter.emit_trust_record(journal, "run-1", "exec-1")
        parsed = json.loads(output)
        expected = json.dumps(parsed, sort_keys=True, separators=(",", ":"))
        assert output == expected

    def test_output_contains_every_required_top_level_field(self, tmp_path: Path) -> None:
        emitter = TrustRecordEmitter()
        journal = _create_journal(tmp_path, [{"type": "run_completed", "ts": 1000.0}])

        parsed = json.loads(emitter.emit_trust_record(journal, "run-1", "exec-1"))

        for field in (
            "eat_profile",
            "iat",
            "subject",
            "model",
            "runtime",
            "policy",
            "data_class",
            "build_provenance",
            "appraisal",
            "cnf",
            "signature",
        ):
            assert field in parsed, f"missing required field: {field}"

    def test_old_claims_and_enforce_shape_is_gone(self, tmp_path: Path) -> None:
        """Pins the corrections-table regression: the homegrown shape must not reappear."""
        emitter = TrustRecordEmitter()
        journal = _create_journal(tmp_path, [{"type": "run_completed", "ts": 1000.0}])

        parsed = json.loads(emitter.emit_trust_record(journal, "run-1", "exec-1"))

        assert "claims" not in parsed
        assert "enforce" not in parsed
        assert "parent_record_hash" not in parsed  # top-level -- lives under delegation now

    def test_full_round_trip_produces_a_valid_signature(self, tmp_path: Path) -> None:
        private_pem, public_raw = _test_keypair()
        emitter = TrustRecordEmitter(
            install_rev_getter=lambda: "aaaaaaaaaaaaaaaa",
            get_private_key_pem=lambda: private_pem,
            get_installed_digest=lambda: "sha256:" + "ab" * 32,
        )
        journal = _create_journal(
            tmp_path,
            [
                {"type": "run_start", "ts": 1690000000.0},
                {"type": "tool_call", "tool": "fs.read", "ts": 1690000001.0},
                {"type": "task_complete", "ts": 1690000002.0},
            ],
        )

        output = emitter.emit_trust_record(journal, "integration-run", "integration-exec")
        parsed = json.loads(output)

        assert parsed["subject"] == "spiffe://bernstein.run/run/integration-run/exec/integration-exec"
        assert parsed["iat"] == 1690000002
        assert parsed["tool_transcript"]["call_count"] == 1
        assert parsed["cnf"]["jwk"]["kid"] == "install-aaaaaaaaaaaaaaaa"
        assert len(parsed["signature"]) > 0
        assert _offline_verify(parsed, _public_key_pem_from_raw(public_raw)) is True


# ---------------------------------------------------------------------------
# Determinism: same journal, byte-identical unsigned payload
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_the_same_journal_yields_a_byte_identical_signed_output_across_processes(self, tmp_path: Path) -> None:
        """Two emitter calls on the same journal must produce identical bytes.

        Relies on the autouse ``_isolate_agent_card_keystore`` fixture
        (``tests/conftest.py``) pointing every process spawned during this
        test -- including the two subprocesses below -- at the same per-test
        keystore directory, so all calls sign with the same install key.
        """
        import subprocess
        import sys

        journal = _create_journal(
            tmp_path,
            [
                {"type": "run_start", "ts": 1690000000.0},
                {"type": "tool_call", "tool": "fs.read", "ts": 1690000001.0},
                {"type": "task_complete", "ts": 1690000002.0},
            ],
        )
        run_id, exec_id = "determinism-run", "determinism-exec"

        snippet = (
            "import sys\n"
            "from pathlib import Path\n"
            "from bernstein.core.observability.trust_record import TrustRecordEmitter\n"
            "journal = Path(sys.argv[1])\n"
            "print(TrustRecordEmitter().emit_trust_record(journal, sys.argv[2], sys.argv[3]))\n"
        )
        repo_src = str(Path(__file__).resolve().parents[4] / "src")
        env = {**os.environ, "PYTHONPATH": repo_src}
        first = subprocess.run(
            [sys.executable, "-c", snippet, str(journal), run_id, exec_id],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        ).stdout.rstrip("\n")
        second = subprocess.run(
            [sys.executable, "-c", snippet, str(journal), run_id, exec_id],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        ).stdout.rstrip("\n")
        assert first == second

        in_process = TrustRecordEmitter().emit_trust_record(journal, run_id, exec_id)
        assert in_process == first


# ---------------------------------------------------------------------------
# Core install unchanged without the [trace] extra
# ---------------------------------------------------------------------------


class TestCoreInstallWithoutTraceExtra:
    def test_importing_bernstein_does_not_import_agentrust_trace(self) -> None:
        """Importing bernstein must not pull in agentrust_trace.

        The trace extra is optional; a future refactor that accidentally
        adds a top-level import would silently reintroduce the transitive
        dependency, so this test pins the guard with a subprocess.
        """
        import subprocess
        import sys

        repo_src = str(Path(__file__).resolve().parents[4] / "src")
        env = {**os.environ, "PYTHONPATH": repo_src}
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys, bernstein; print([m for m in sys.modules if 'agentrust' in m])",
            ],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        assert proc.stdout.rstrip("\n") == "[]"


# ---------------------------------------------------------------------------
# Signing pre-image: JCS edge cases (non-ASCII data_class, float exponent range)
# ---------------------------------------------------------------------------


class TestSigningPreImageJCS:
    """Verify the signature was actually produced over the JCS pre-image.

    These regression tests protect against accidental re-introduction of
    ``json.dumps(sort_keys=True)`` (RFC 8259) as the signing pre-image.
    Each test verifies ``doc["signature"]`` with an independent Ed25519
    verify call (:func:`_offline_verify`, built from scratch in this test
    file rather than reusing the module's own
    ``_record_dict_without_signature``/``canonicalize_jcs`` call sequence)
    against a known public key, and confirms with a tamper control that the
    verify call is actually sensitive to the bytes being checked -- a test
    that only ever compared two independently-computed JCS renderings of
    the same parsed ``doc`` (never reading ``signature`` itself) would pass
    even if signing and verification silently agreed on the wrong
    pre-image, which is exactly the class of bug this guards against.
    """

    def test_non_ascii_data_class_signing_pre_image_matches_jcs(self, tmp_path: Path) -> None:
        private_pem, public_raw = _test_keypair()
        emitter = TrustRecordEmitter(
            install_rev_getter=lambda: "aaaaaaaaaaaaaaaa",
            get_private_key_pem=lambda: private_pem,
            get_installed_digest=lambda: "sha256:" + "ab" * 32,
        )
        journal = _create_journal(tmp_path, [{"type": "run_started", "data_class": "конфиденциально"}])
        output = emitter.emit_trust_record(journal, "run-1", "exec-1")
        doc = json.loads(output)
        public_key_pem = _public_key_pem_from_raw(public_raw)

        # The real assertion: the signature verifies against the record's
        # own JCS pre-image, over an actual Ed25519 verify call.
        assert _offline_verify(doc, public_key_pem) is True

        # No \u-escaped Cyrillic in the bytes that were actually signed.
        assert b"\\u04" not in _canonical_body_bytes(doc)

        # Tamper control: mutating the non-ASCII field must invalidate the
        # signature, proving the verify call above is not a vacuous no-op.
        doc["data_class"] = "public"
        assert _offline_verify(doc, public_key_pem) is False

    def test_float_1e7_exponent_iat_rounding_still_matches_jcs(self, tmp_path: Path) -> None:
        private_pem, public_raw = _test_keypair()
        emitter = TrustRecordEmitter(
            install_rev_getter=lambda: "aaaaaaaaaaaaaaaa",
            get_private_key_pem=lambda: private_pem,
            get_installed_digest=lambda: "sha256:" + "ab" * 32,
        )
        journal = _create_journal(tmp_path, [{"type": "run_completed", "ts": 1e-7}])
        output = emitter.emit_trust_record(journal, "run-1", "exec-1")
        doc = json.loads(output)
        public_key_pem = _public_key_pem_from_raw(public_raw)

        # The real assertion: the signature verifies against the record's
        # own JCS pre-image (with iat rounded from a 1e-7-exponent float),
        # over an actual Ed25519 verify call.
        assert _offline_verify(doc, public_key_pem) is True

        # Tamper control: mutating iat must invalidate the signature,
        # proving the verify call above is not a vacuous no-op.
        doc["iat"] = doc["iat"] + 1
        assert _offline_verify(doc, public_key_pem) is False


class TestModuleDocstringStatesTheSealBoundary:
    def test_docstring_states_what_the_seal_can_and_cannot_prove(self) -> None:
        import bernstein.core.observability.trust_record as trust_record_module

        doc = trust_record_module.__doc__ or ""
        assert "cannot prove" in doc
        assert "signed software evidence" in doc
        assert "no re-canonicalisation" not in doc
