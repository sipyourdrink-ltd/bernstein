"""Unit tests for the multi-tenant HMAC-chained audit-log export.

Covers the ticket's hard constraints:

* **Determinism** - same input → byte-identical bundle.
* **Tenant filter** - only events with the matching ``tenant_id`` leak
  through.
* **Chain integrity** - :func:`verify_tenant_slice` passes on a clean
  slice; a one-byte flip flips it to ``ok=False``.
* **Cross-tenant leakage** - a tampered ``tenant_id`` in a slice is
  detected.
* **Empty window safe** - no events for tenant produces an empty-but-
  verifiable slice.

The tests use the same HMAC key plumbing as the production
:class:`AuditLog` so the slice exercises the real keying surface.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bernstein.core.security.audit import AuditLog
from bernstein.core.security.audit_multitenant import (
    EXPORT_SCHEMA_VERSION,
    _filter_tenant_events,
    export_tenant_slice,
    verify_tenant_slice,
)

# A deterministic byte key - easier to reason about than a generated hex key.
_TEST_KEY: bytes = b"x" * 32


def _seed_two_tenants(audit_dir: Path) -> AuditLog:
    """Write a small chain mixing two tenants + one untagged ('default').

    Returns:
        The :class:`AuditLog` that wrote the events (still keyed to
        ``_TEST_KEY``).
    """
    audit_dir.mkdir(parents=True, exist_ok=True)
    log = AuditLog(audit_dir, key=_TEST_KEY)
    log.log("task.created", "alice", "task", "T-1", {"tenant_id": "acme"})
    log.log("agent.spawned", "orchestrator", "agent", "A-1", {"tenant_id": "acme"})
    log.log("task.created", "bob", "task", "T-2", {"tenant_id": "globex"})
    log.log("legacy.event", "system", "task", "T-3", {})  # → tenant 'default'
    log.log("task.completed", "alice", "task", "T-1", {"tenant_id": "acme"})
    return log


def _today_window() -> tuple[str, str]:
    """Return an ``[since, until)`` pair covering today (UTC)."""
    today = datetime.now(tz=UTC).date()
    since = f"{today.isoformat()}T00:00:00+00:00"
    until = f"{(today + timedelta(days=1)).isoformat()}T00:00:00+00:00"
    return since, until


def _in_window_timestamp(since: str, until: str) -> str:
    """Return a timestamp the window filter accepts."""
    assert since < until
    return since


def _exported_bundle(tmp_path: Path) -> dict:
    """Export a clean acme slice and return it as a mutable dict."""
    audit_dir = tmp_path / ".sdd" / "audit"
    _seed_two_tenants(audit_dir)
    since, until = _today_window()
    export = export_tenant_slice(
        audit_dir=audit_dir,
        tenant_id="acme",
        since=since,
        until=until,
        key=_TEST_KEY,
        output_dir=tmp_path / "out",
        write=True,
    )
    assert export.bundle_path is not None
    return json.loads(export.bundle_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Tenant filter
# ---------------------------------------------------------------------------


class TestTenantFilter:
    """Only events tagged with the requested tenant_id leak through."""

    def test_acme_isolation(self, tmp_path: Path) -> None:
        audit_dir = tmp_path / ".sdd" / "audit"
        _seed_two_tenants(audit_dir)
        since, until = _today_window()

        export = export_tenant_slice(
            audit_dir=audit_dir,
            tenant_id="acme",
            since=since,
            until=until,
            key=_TEST_KEY,
            output_dir=tmp_path / "out",
            write=True,
        )
        assert export.event_count == 3
        assert export.tenant_id == "acme"
        # Every emitted event carries tenant_id=acme.
        bundle = json.loads(export.bundle_bytes.decode("utf-8"))
        for event in bundle["events"]:
            assert event["details"]["tenant_id"] == "acme"

    def test_globex_isolation(self, tmp_path: Path) -> None:
        audit_dir = tmp_path / ".sdd" / "audit"
        _seed_two_tenants(audit_dir)
        since, until = _today_window()

        export = export_tenant_slice(
            audit_dir=audit_dir,
            tenant_id="globex",
            since=since,
            until=until,
            key=_TEST_KEY,
            output_dir=tmp_path / "out",
            write=True,
        )
        assert export.event_count == 1
        bundle = json.loads(export.bundle_bytes.decode("utf-8"))
        assert all(e["details"]["tenant_id"] == "globex" for e in bundle["events"])

    def test_untagged_events_collapse_to_default_tenant(self, tmp_path: Path) -> None:
        """Events without ``details.tenant_id`` belong to 'default'.

        Matches :func:`bernstein.core.security.tenanting.normalize_tenant_id`.
        Critical for backwards compatibility - operators who roll
        multi-tenant out gradually keep their pre-existing chain visible.
        """
        audit_dir = tmp_path / ".sdd" / "audit"
        _seed_two_tenants(audit_dir)
        since, until = _today_window()

        export = export_tenant_slice(
            audit_dir=audit_dir,
            tenant_id="default",
            since=since,
            until=until,
            key=_TEST_KEY,
            output_dir=tmp_path / "out",
            write=True,
        )
        assert export.event_count == 1


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    """Same inputs → byte-identical output. Required for spot-audit replay."""

    def test_byte_identical_rebuild(self, tmp_path: Path) -> None:
        audit_dir = tmp_path / ".sdd" / "audit"
        _seed_two_tenants(audit_dir)
        since, until = _today_window()

        first = export_tenant_slice(
            audit_dir=audit_dir,
            tenant_id="acme",
            since=since,
            until=until,
            key=_TEST_KEY,
            output_dir=tmp_path / "out1",
            write=True,
        )
        second = export_tenant_slice(
            audit_dir=audit_dir,
            tenant_id="acme",
            since=since,
            until=until,
            key=_TEST_KEY,
            output_dir=tmp_path / "out2",
            write=True,
        )

        # In-memory bytes match.
        assert first.bundle_bytes == second.bundle_bytes
        # Cryptographic anchors match.
        assert first.head_hmac == second.head_hmac
        assert first.head_sha256 == second.head_sha256
        assert first.sha256 == second.sha256
        # On-disk bytes match.
        assert first.bundle_path is not None
        assert second.bundle_path is not None
        assert first.bundle_path.read_bytes() == second.bundle_path.read_bytes()

    def test_offline_anchor_with_pinned_timestamp_is_deterministic(self, tmp_path: Path) -> None:
        """Air-gap mode is deterministic when the operator pins the anchor ts.

        This guards the air-gap branch: ``signature_kind=hmac-chain+
        offline-anchor`` defaults the anchor timestamp to ``now()`` -
        which is non-deterministic. Operators chasing byte-stable
        bundles must pin ``offline_anchor_iso``.
        """
        audit_dir = tmp_path / ".sdd" / "audit"
        _seed_two_tenants(audit_dir)
        since, until = _today_window()
        pinned = "2026-08-01T00:00:00Z"

        first = export_tenant_slice(
            audit_dir=audit_dir,
            tenant_id="acme",
            since=since,
            until=until,
            key=_TEST_KEY,
            signature_kind="hmac-chain+offline-anchor",
            offline_anchor_iso=pinned,
            output_dir=tmp_path / "a",
            write=True,
        )
        second = export_tenant_slice(
            audit_dir=audit_dir,
            tenant_id="acme",
            since=since,
            until=until,
            key=_TEST_KEY,
            signature_kind="hmac-chain+offline-anchor",
            offline_anchor_iso=pinned,
            output_dir=tmp_path / "b",
            write=True,
        )
        assert first.bundle_bytes == second.bundle_bytes


# ---------------------------------------------------------------------------
# Chain integrity
# ---------------------------------------------------------------------------


class TestChainIntegrity:
    """The slice-local HMAC chain must verify offline."""

    def test_verify_passes_on_clean_slice(self, tmp_path: Path) -> None:
        audit_dir = tmp_path / ".sdd" / "audit"
        _seed_two_tenants(audit_dir)
        since, until = _today_window()

        export = export_tenant_slice(
            audit_dir=audit_dir,
            tenant_id="acme",
            since=since,
            until=until,
            key=_TEST_KEY,
            output_dir=tmp_path / "out",
            write=True,
        )
        assert export.bundle_path is not None
        result = verify_tenant_slice(export.bundle_path, key=_TEST_KEY)
        assert result.ok, result.errors
        assert result.bundle["schema_version"] == EXPORT_SCHEMA_VERSION

    def test_verify_passes_on_in_memory_bytes(self, tmp_path: Path) -> None:
        """Verifier accepts raw bytes (no disk read) and parsed dicts alike."""
        audit_dir = tmp_path / ".sdd" / "audit"
        _seed_two_tenants(audit_dir)
        since, until = _today_window()

        export = export_tenant_slice(
            audit_dir=audit_dir,
            tenant_id="acme",
            since=since,
            until=until,
            key=_TEST_KEY,
            write=False,
        )
        result_from_bytes = verify_tenant_slice(export.bundle_bytes, key=_TEST_KEY)
        assert result_from_bytes.ok, result_from_bytes.errors

        as_dict = json.loads(export.bundle_bytes.decode("utf-8"))
        result_from_dict = verify_tenant_slice(as_dict, key=_TEST_KEY)
        assert result_from_dict.ok, result_from_dict.errors

    def test_one_byte_flip_in_event_breaks_verification(self, tmp_path: Path) -> None:
        """Mutate one byte inside an event's resource_id → verifier fails.

        Targets a byte unambiguously inside the chain-covered region. The
        flip changes the event payload that feeds HMAC, so chain
        verification fails on the affected event.
        """
        audit_dir = tmp_path / ".sdd" / "audit"
        _seed_two_tenants(audit_dir)
        since, until = _today_window()

        export = export_tenant_slice(
            audit_dir=audit_dir,
            tenant_id="acme",
            since=since,
            until=until,
            key=_TEST_KEY,
            output_dir=tmp_path / "out",
            write=True,
        )
        assert export.bundle_path is not None

        # Flip the resource_id 'T-1' → 'X-1' inside the events array.
        # That string only appears in event payloads (not in metadata).
        original = export.bundle_path.read_bytes()
        target = b'"resource_id":"T-1"'
        idx = original.find(target)
        assert idx >= 0, "expected resource_id payload in bundle"
        flipped = bytearray(original)
        flipped[idx + len(b'"resource_id":"')] = ord("X")
        export.bundle_path.write_bytes(bytes(flipped))

        result = verify_tenant_slice(export.bundle_path, key=_TEST_KEY)
        assert not result.ok
        assert result.errors  # at least one human-readable failure
        joined = " ".join(result.errors)
        # Either the chain HMAC re-derivation or the head_sha256 anchor
        # catches the flip.
        assert "HMAC mismatch" in joined or "head_sha256 mismatch" in joined

    def test_one_byte_flip_in_metadata_breaks_anchor(self, tmp_path: Path) -> None:
        """Flip a byte in audit_window → schema sanity check fails.

        ``audit_window`` is bundle metadata, not chain-covered. The
        verifier still rejects it because the envelope checks require
        well-formed since/until strings and since < until.
        """
        audit_dir = tmp_path / ".sdd" / "audit"
        _seed_two_tenants(audit_dir)
        since, until = _today_window()

        export = export_tenant_slice(
            audit_dir=audit_dir,
            tenant_id="acme",
            since=since,
            until=until,
            key=_TEST_KEY,
            write=False,
        )
        bundle = json.loads(export.bundle_bytes.decode("utf-8"))
        # Make since > until so the envelope check rejects.
        bundle["audit_window"]["since"] = "2099-01-01T00:00:00+00:00"
        result = verify_tenant_slice(bundle, key=_TEST_KEY)
        assert not result.ok

    def test_wrong_key_fails_verification(self, tmp_path: Path) -> None:
        audit_dir = tmp_path / ".sdd" / "audit"
        _seed_two_tenants(audit_dir)
        since, until = _today_window()

        export = export_tenant_slice(
            audit_dir=audit_dir,
            tenant_id="acme",
            since=since,
            until=until,
            key=_TEST_KEY,
            write=False,
        )
        result = verify_tenant_slice(export.bundle_bytes, key=b"y" * 32)
        assert not result.ok
        # The chain check should be the failing one.
        assert any("HMAC mismatch" in e for e in result.errors)


# ---------------------------------------------------------------------------
# Cross-tenant tamper detection
# ---------------------------------------------------------------------------


class TestCrossTenantTamperDetection:
    """A flipped tenant_id inside a slice must be caught."""

    def test_tampered_tenant_id_detected(self, tmp_path: Path) -> None:
        audit_dir = tmp_path / ".sdd" / "audit"
        _seed_two_tenants(audit_dir)
        since, until = _today_window()

        export = export_tenant_slice(
            audit_dir=audit_dir,
            tenant_id="acme",
            since=since,
            until=until,
            key=_TEST_KEY,
            write=False,
        )
        # Parse, mutate, re-serialise - simulate an attacker who edits
        # the bundle JSON manually.
        bundle = json.loads(export.bundle_bytes.decode("utf-8"))
        bundle["events"][0]["details"]["tenant_id"] = "globex"
        result = verify_tenant_slice(bundle, key=_TEST_KEY)
        assert not result.ok
        # Either tenant purity or the chain mismatch (since the HMAC
        # covers details) flags the tamper.
        joined = " ".join(result.errors)
        assert "tenant_id mismatch" in joined or "HMAC mismatch" in joined

    def test_top_level_tenant_id_flip_detected(self, tmp_path: Path) -> None:
        """Flipping only the top-level tenant_id (header) must still fail.

        The verifier walks every event and confirms each one carries the
        declared tenant_id. A top-level flip lights up purity.
        """
        audit_dir = tmp_path / ".sdd" / "audit"
        _seed_two_tenants(audit_dir)
        since, until = _today_window()

        export = export_tenant_slice(
            audit_dir=audit_dir,
            tenant_id="acme",
            since=since,
            until=until,
            key=_TEST_KEY,
            write=False,
        )
        bundle = json.loads(export.bundle_bytes.decode("utf-8"))
        bundle["tenant_id"] = "globex"
        result = verify_tenant_slice(bundle, key=_TEST_KEY)
        assert not result.ok
        joined = " ".join(result.errors)
        assert "tenant_id mismatch" in joined


# ---------------------------------------------------------------------------
# Empty window safety
# ---------------------------------------------------------------------------


class TestEmptyWindowSafe:
    """No events for the tenant → produce an empty-but-verifiable slice."""

    def test_empty_window_has_genesis_anchors(self, tmp_path: Path) -> None:
        audit_dir = tmp_path / ".sdd" / "audit"
        audit_dir.mkdir(parents=True, exist_ok=True)
        # No events at all.
        since, until = _today_window()

        export = export_tenant_slice(
            audit_dir=audit_dir,
            tenant_id="acme",
            since=since,
            until=until,
            key=_TEST_KEY,
            output_dir=tmp_path / "out",
            write=True,
        )
        assert export.event_count == 0
        assert export.head_hmac == "0" * 64
        # head_sha256 of empty JSONL is the SHA-256 of the empty string.
        import hashlib

        assert export.head_sha256 == hashlib.sha256(b"").hexdigest()

        result = verify_tenant_slice(export.bundle_bytes, key=_TEST_KEY)
        assert result.ok, result.errors

    def test_unknown_tenant_id_returns_empty_safe(self, tmp_path: Path) -> None:
        """Tenant id that never appears in the log is treated as empty.

        Same invariants as the empty-log case: head HMAC = genesis,
        verifier returns ok.
        """
        audit_dir = tmp_path / ".sdd" / "audit"
        _seed_two_tenants(audit_dir)
        since, until = _today_window()

        export = export_tenant_slice(
            audit_dir=audit_dir,
            tenant_id="initech",  # never seen
            since=since,
            until=until,
            key=_TEST_KEY,
            write=False,
        )
        assert export.event_count == 0
        assert export.head_hmac == "0" * 64
        result = verify_tenant_slice(export.bundle_bytes, key=_TEST_KEY)
        assert result.ok, result.errors


# ---------------------------------------------------------------------------
# Signature variants
# ---------------------------------------------------------------------------


class TestSignatureKinds:
    """Each signature kind round-trips through the verifier cleanly."""

    def test_hmac_chain_only(self, tmp_path: Path) -> None:
        audit_dir = tmp_path / ".sdd" / "audit"
        _seed_two_tenants(audit_dir)
        since, until = _today_window()

        export = export_tenant_slice(
            audit_dir=audit_dir,
            tenant_id="acme",
            since=since,
            until=until,
            key=_TEST_KEY,
            signature_kind="hmac-chain-only",
            write=False,
        )
        result = verify_tenant_slice(export.bundle_bytes, key=_TEST_KEY)
        assert result.ok, result.errors

    def test_offline_anchor_self_consistent(self, tmp_path: Path) -> None:
        audit_dir = tmp_path / ".sdd" / "audit"
        _seed_two_tenants(audit_dir)
        since, until = _today_window()

        export = export_tenant_slice(
            audit_dir=audit_dir,
            tenant_id="acme",
            since=since,
            until=until,
            key=_TEST_KEY,
            signature_kind="hmac-chain+offline-anchor",
            offline_anchor_iso="2026-08-01T00:00:00Z",
            write=False,
        )
        result = verify_tenant_slice(export.bundle_bytes, key=_TEST_KEY)
        assert result.ok, result.errors
        bundle = json.loads(export.bundle_bytes.decode("utf-8"))
        anchor = bundle["signature"]["offline_anchor"]
        assert anchor["anchored_at"] == "2026-08-01T00:00:00Z"

    def test_offline_anchor_tampered_anchor_detected(self, tmp_path: Path) -> None:
        audit_dir = tmp_path / ".sdd" / "audit"
        _seed_two_tenants(audit_dir)
        since, until = _today_window()

        export = export_tenant_slice(
            audit_dir=audit_dir,
            tenant_id="acme",
            since=since,
            until=until,
            key=_TEST_KEY,
            signature_kind="hmac-chain+offline-anchor",
            offline_anchor_iso="2026-08-01T00:00:00Z",
            write=False,
        )
        bundle = json.loads(export.bundle_bytes.decode("utf-8"))
        bundle["signature"]["offline_anchor"]["anchor_sha256"] = "0" * 64
        result = verify_tenant_slice(bundle, key=_TEST_KEY)
        assert not result.ok
        assert any("offline_anchor" in e for e in result.errors)

    def test_rfc3161_requires_token(self, tmp_path: Path) -> None:
        audit_dir = tmp_path / ".sdd" / "audit"
        _seed_two_tenants(audit_dir)
        since, until = _today_window()

        with pytest.raises(ValueError, match="rfc3161_token_b64"):
            export_tenant_slice(
                audit_dir=audit_dir,
                tenant_id="acme",
                since=since,
                until=until,
                key=_TEST_KEY,
                signature_kind="hmac-chain+rfc3161",
                rfc3161_token_b64=None,
                write=False,
            )

    def test_rfc3161_token_round_trips(self, tmp_path: Path) -> None:
        """The verifier accepts a valid base64 token; rejects garbage."""
        import base64

        audit_dir = tmp_path / ".sdd" / "audit"
        _seed_two_tenants(audit_dir)
        since, until = _today_window()

        token = base64.b64encode(b"fake-tsa-der-bytes").decode("ascii")
        export = export_tenant_slice(
            audit_dir=audit_dir,
            tenant_id="acme",
            since=since,
            until=until,
            key=_TEST_KEY,
            signature_kind="hmac-chain+rfc3161",
            rfc3161_token_b64=token,
            rfc3161_tsa_url="https://freetsa.example/tsa",
            write=False,
        )
        result = verify_tenant_slice(export.bundle_bytes, key=_TEST_KEY)
        assert result.ok, result.errors
        bundle = json.loads(export.bundle_bytes.decode("utf-8"))
        assert bundle["signature"]["rfc3161_token_b64"] == token

        # Tamper: replace token with non-base64 garbage.
        bundle["signature"]["rfc3161_token_b64"] = "not!valid!base64!"
        garbage_result = verify_tenant_slice(bundle, key=_TEST_KEY)
        assert not garbage_result.ok


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestInputValidation:
    """Operator misuse must surface fast."""

    def test_since_must_be_less_than_until(self, tmp_path: Path) -> None:
        audit_dir = tmp_path / ".sdd" / "audit"
        audit_dir.mkdir(parents=True)
        with pytest.raises(ValueError, match="since"):
            export_tenant_slice(
                audit_dir=audit_dir,
                tenant_id="acme",
                since="2026-08-02T00:00:00+00:00",
                until="2026-08-01T00:00:00+00:00",
                key=_TEST_KEY,
                write=False,
            )

    def test_empty_tenant_collapses_to_default(self, tmp_path: Path) -> None:
        """Empty/whitespace tenant_id collapses to ``default``.

        Matches :func:`bernstein.core.security.tenanting.normalize_tenant_id`
        and avoids a footgun where the operator passes ``""`` and accidentally
        gets a slice of *every* untagged event without warning.
        """
        audit_dir = tmp_path / ".sdd" / "audit"
        _seed_two_tenants(audit_dir)
        since, until = _today_window()

        export = export_tenant_slice(
            audit_dir=audit_dir,
            tenant_id="   ",
            since=since,
            until=until,
            key=_TEST_KEY,
            write=False,
        )
        assert export.tenant_id == "default"


class TestMalformedTenantValuesAreReportedNotCoerced:
    """A bundle's tenant fields are read as written.

    `str()` on a stored `true` or `123` yields a string the tenant rules
    accept, so a bundle declaring one verified clean under a tenant name
    nothing wrote it for.
    """

    @pytest.mark.parametrize("malformed", [True, 123, "../escape", "tenant with spaces"])
    def test_unreadable_declared_tenant_is_an_error_not_a_pass(self, tmp_path: Path, malformed: object) -> None:
        audit_dir = tmp_path / ".sdd" / "audit"
        _seed_two_tenants(audit_dir)
        since, until = _today_window()
        export = export_tenant_slice(
            audit_dir=audit_dir,
            tenant_id="acme",
            since=since,
            until=until,
            key=_TEST_KEY,
            output_dir=tmp_path / "out",
            write=True,
        )
        assert export.bundle_path is not None
        bundle = json.loads(export.bundle_path.read_text(encoding="utf-8"))
        bundle["tenant_id"] = malformed

        result = verify_tenant_slice(bundle, key=_TEST_KEY)

        assert not result.ok
        assert any("tenant" in err for err in result.errors)

    def test_verification_returns_findings_rather_than_raising(self, tmp_path: Path) -> None:
        """A verifier that raises leaves the caller with no findings at all."""
        audit_dir = tmp_path / ".sdd" / "audit"
        _seed_two_tenants(audit_dir)
        since, until = _today_window()
        export = export_tenant_slice(
            audit_dir=audit_dir,
            tenant_id="acme",
            since=since,
            until=until,
            key=_TEST_KEY,
            output_dir=tmp_path / "out",
            write=True,
        )
        assert export.bundle_path is not None
        bundle = json.loads(export.bundle_path.read_text(encoding="utf-8"))
        for event in bundle.get("events") or []:
            event.setdefault("details", {})["tenant_id"] = "../escape"

        result = verify_tenant_slice(bundle, key=_TEST_KEY)

        assert not result.ok
        assert result.errors


class TestABundleMustDeclareAUsableTenant:
    """`tenant_id` was checked for presence only, never for shape."""

    @pytest.mark.parametrize("declared", [None, "", "   ", 123, True, ["acme"], {"id": "acme"}, "../escape"])
    def test_unusable_declaration_is_a_schema_error(self, tmp_path: Path, declared: object) -> None:
        """Reported as a malformed declaration, not defaulted and then compared.

        Reading an unusable declaration as the default tenant left a bundle
        with no events verifying clean, and one with events failing for the
        wrong reason -- a mismatch against a tenant the bundle never declared.
        """
        bundle = _exported_bundle(tmp_path)
        bundle["tenant_id"] = declared

        result = verify_tenant_slice(bundle, key=_TEST_KEY)

        assert not result.ok
        schema_phrases = ("must be a string", "must not be blank", "not a usable tenant identifier")
        assert any(phrase in err for err in result.errors for phrase in schema_phrases)

    def test_an_empty_slice_cannot_hide_an_unusable_declaration(self, tmp_path: Path) -> None:
        """With no events there is nothing to mismatch against."""
        bundle = _exported_bundle(tmp_path)
        bundle["events"] = []
        bundle["event_count"] = 0
        bundle["tenant_id"] = None

        result = verify_tenant_slice(bundle, key=_TEST_KEY)

        assert not result.ok
        assert any("tenant_id" in err for err in result.errors)

    def test_a_missing_declaration_is_still_a_schema_error(self, tmp_path: Path) -> None:
        bundle = _exported_bundle(tmp_path)
        del bundle["tenant_id"]

        result = verify_tenant_slice(bundle, key=_TEST_KEY)

        assert not result.ok
        assert any("tenant_id" in err for err in result.errors)


class TestVerificationReturnsFindingsForEveryBundleShape:
    """A verifier that raises hands the caller an exception, not findings."""

    @pytest.mark.parametrize("event", [[1, 2, 3], "a string", 42, None])
    def test_a_non_object_event_is_reported_not_raised(self, tmp_path: Path, event: object) -> None:
        bundle = _exported_bundle(tmp_path)
        bundle["events"] = [event]

        result = verify_tenant_slice(bundle, key=_TEST_KEY)

        assert not result.ok
        assert result.errors

    @pytest.mark.parametrize("details", [[], ["x"], "a string", 42])
    def test_a_non_object_details_is_reported_not_raised(self, tmp_path: Path, details: object) -> None:
        bundle = _exported_bundle(tmp_path)
        for evt in bundle["events"]:
            evt["details"] = details

        result = verify_tenant_slice(bundle, key=_TEST_KEY)

        assert not result.ok
        assert result.errors


class TestExportRefusesToDropUnreadableEvidence:
    """An export that silently omits records looks complete and is not."""

    def test_an_unreadable_in_window_tenant_stops_the_export(self, tmp_path: Path) -> None:
        audit_dir = tmp_path / ".sdd" / "audit"
        _seed_two_tenants(audit_dir)
        since, until = _today_window()

        segment = next(iter(audit_dir.glob("*.jsonl")))
        lines = segment.read_text(encoding="utf-8").splitlines()
        first = json.loads(lines[0])
        first.setdefault("details", {})["tenant_id"] = "../escape"
        lines[0] = json.dumps(first)
        segment.write_text("\n".join(lines) + "\n", encoding="utf-8")

        with pytest.raises(ValueError, match="unreadable"):
            export_tenant_slice(
                audit_dir=audit_dir,
                tenant_id="acme",
                since=since,
                until=until,
                key=_TEST_KEY,
                output_dir=tmp_path / "out",
                write=True,
            )

    def test_a_clean_log_still_exports(self, tmp_path: Path) -> None:
        audit_dir = tmp_path / ".sdd" / "audit"
        _seed_two_tenants(audit_dir)
        since, until = _today_window()

        export = export_tenant_slice(
            audit_dir=audit_dir,
            tenant_id="acme",
            since=since,
            until=until,
            key=_TEST_KEY,
            output_dir=tmp_path / "out",
            write=True,
        )

        assert export.bundle_path is not None
        assert verify_tenant_slice(export.bundle_path, key=_TEST_KEY).ok


class TestNonMappingDetailsIsUnreadableNotDefault:
    """A details field nothing can be read out of names no tenant.

    Reading it as an absent details filed the event under the default
    tenant's evidence, which is an attribution no writer made.
    """

    @pytest.mark.parametrize("details", [["x"], [], "a string", 42, True])
    def test_filter_quarantines_a_non_mapping_details(self, details: object) -> None:
        since, until = _today_window()
        stamp = _in_window_timestamp(since, until)
        events = [
            {"timestamp": stamp, "hmac": "a", "details": {"tenant_id": "acme"}},
            {"timestamp": stamp, "hmac": "b", "details": details},
        ]

        matched, unreadable = _filter_tenant_events(events, "default", since, until)

        assert matched == []
        assert unreadable == [1]

    @pytest.mark.parametrize("details", [["x"], "a string", 42])
    def test_a_non_mapping_details_never_joins_another_tenant_slice(self, details: object) -> None:
        since, until = _today_window()
        stamp = _in_window_timestamp(since, until)
        events = [{"timestamp": stamp, "hmac": "b", "details": details}]

        for tenant in ("default", "acme", "globex"):
            matched, unreadable = _filter_tenant_events(events, tenant, since, until)
            assert matched == []
            assert unreadable == [0]

    def test_an_absent_details_still_reads_as_the_default_tenant(self) -> None:
        """Absent states nothing; unreadable states something illegible."""
        since, until = _today_window()
        stamp = _in_window_timestamp(since, until)
        events = [
            {"timestamp": stamp, "hmac": "a"},
            {"timestamp": stamp, "hmac": "b", "details": {}},
            {"timestamp": stamp, "hmac": "c", "details": None},
        ]

        matched, unreadable = _filter_tenant_events(events, "default", since, until)

        assert unreadable == []
        assert len(matched) == 3

    def test_export_refuses_a_slice_holding_a_non_mapping_details(self, tmp_path: Path) -> None:
        audit_dir = tmp_path / ".sdd" / "audit"
        _seed_two_tenants(audit_dir)
        since, until = _today_window()

        segment = next(iter(audit_dir.glob("*.jsonl")))
        lines = segment.read_text(encoding="utf-8").splitlines()
        first = json.loads(lines[0])
        first["details"] = ["not", "a", "mapping"]
        lines[0] = json.dumps(first)
        segment.write_text("\n".join(lines) + "\n", encoding="utf-8")

        with pytest.raises(ValueError, match="unreadable"):
            export_tenant_slice(
                audit_dir=audit_dir,
                tenant_id="acme",
                since=since,
                until=until,
                key=_TEST_KEY,
                output_dir=tmp_path / "out",
                write=True,
            )
