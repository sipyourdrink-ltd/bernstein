"""Tests for the provenance-verified release update advisory (issue #2942).

Every test here is hermetic: the "release feed" is a signed document built
in-process into ``tmp_path``. Nothing reaches the network, and one test
asserts that positively by installing a urlopen that fails the test if it is
ever called.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import TYPE_CHECKING, Any

import pytest

from bernstein.core.distribution.update_advisory import (
    ENV_RELEASE_FEED,
    ENV_TRUST_ROOT,
    ENV_UPDATE_CHECK,
    SURFACE_FEATURE,
    SURFACE_SECURITY,
    SURFACE_VERIFICATION,
    ReleaseEntry,
    ReleaseFeedError,
    VersionPin,
    build_install_receipt,
    build_release_feed_document,
    build_update_advisory,
    cache_is_fresh,
    classify_surface_delta,
    compare_versions,
    fetch_release_feed,
    load_cached_advisory,
    load_trust_root,
    parse_release_feed,
    pin_blocks,
    previous_receipted_version,
    read_version_pin,
    receipt_sha256,
    resolve_check_permission,
    seal_advisory,
    select_candidate,
    sha256_file,
    store_cached_advisory,
    store_receipt,
    verify_advisory_document,
    verify_release_feed_document,
    verify_wheel_against_advisory,
    write_version_pin,
)
from bernstein.core.security.agent_card_signer import generate_ed25519_keypair

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------

_WHEEL_BODY = b"pretend wheel bytes"
_WHEEL_SHA256 = "e5f1c1e9bb0e0b4ee0f8b8f37b71c6b41e4a2a1b6d1e6b1a1a1d1c1b1a191817"


@pytest.fixture
def keypair() -> tuple[bytes, str]:
    private_pem, public_pem = generate_ed25519_keypair()
    return private_pem, public_pem.decode("ascii")


def _entry(version: str, surface: str, *, digest: str | None = None, yanked: bool = False) -> ReleaseEntry:
    return ReleaseEntry(
        version=version,
        wheel_name=f"bernstein-{version}-py3-none-any.whl",
        wheel_sha256=digest or ("a" * 63 + version[-1]),
        surface=surface,
        released_at=f"2026-0{version[0]}-01T00:00:00Z",
        yanked=yanked,
    )


def _feed_document(keypair: tuple[bytes, str], entries: list[ReleaseEntry] | None = None) -> dict[str, Any]:
    private_pem, public_pem = keypair
    releases = (
        entries
        if entries is not None
        else [
            _entry("3.10.0", SURFACE_FEATURE),
            _entry("3.11.0", SURFACE_VERIFICATION),
            _entry("3.12.0", SURFACE_SECURITY),
        ]
    )
    return build_release_feed_document(
        releases,
        private_key_pem=private_pem,
        public_key_pem=public_pem,
        generated_at="2026-07-01T00:00:00Z",
    )


# ---------------------------------------------------------------------------
# Feed parsing
# ---------------------------------------------------------------------------


class TestFeedParsing:
    def test_round_trip(self, keypair: tuple[bytes, str]) -> None:
        feed = parse_release_feed(_feed_document(keypair)["feed"])
        assert [e.version for e in feed.releases] == ["3.10.0", "3.11.0", "3.12.0"]
        assert feed.entry_for("3.11.0") is not None
        assert feed.entry_for("9.9.9") is None

    def test_unknown_surface_refused(self, keypair: tuple[bytes, str]) -> None:
        doc = _feed_document(keypair)
        doc["feed"]["releases"][0]["surface"] = "marketing"
        with pytest.raises(ReleaseFeedError, match="surface"):
            parse_release_feed(doc["feed"])

    def test_bad_hash_refused(self, keypair: tuple[bytes, str]) -> None:
        doc = _feed_document(keypair)
        doc["feed"]["releases"][0]["wheel_sha256"] = "nope"
        with pytest.raises(ReleaseFeedError, match="64 lowercase hex"):
            parse_release_feed(doc["feed"])

    def test_schema_version_pinned(self, keypair: tuple[bytes, str]) -> None:
        doc = _feed_document(keypair)
        doc["feed"]["schema_version"] = 99
        with pytest.raises(ReleaseFeedError, match="schema_version"):
            parse_release_feed(doc["feed"])

    def test_generated_feed_is_deterministic(self, keypair: tuple[bytes, str]) -> None:
        first = _feed_document(keypair)
        second = _feed_document(keypair)
        assert first["feed_sha256"] == second["feed_sha256"]


# ---------------------------------------------------------------------------
# Feed verification: the tamper refusal
# ---------------------------------------------------------------------------


class TestFeedVerification:
    def test_well_formed_feed_verifies(self, keypair: tuple[bytes, str]) -> None:
        _, public_pem = keypair
        result = verify_release_feed_document(_feed_document(keypair), trust_root_pem=public_pem)
        assert result.ok
        assert result.feed is not None

    def test_flipped_byte_is_refused(self, keypair: tuple[bytes, str]) -> None:
        """A single altered character in the signed body must refuse, not warn."""
        _, public_pem = keypair
        doc = _feed_document(keypair)
        doc["feed"]["releases"][2]["wheel_sha256"] = "b" + doc["feed"]["releases"][2]["wheel_sha256"][1:]
        result = verify_release_feed_document(doc, trust_root_pem=public_pem)
        assert not result.ok
        assert result.feed is None
        assert "content hash" in result.reason

    def test_recomputed_hash_but_forged_body_still_refused(self, keypair: tuple[bytes, str]) -> None:
        """Fixing the content hash after tampering does not rescue the signature."""
        import hashlib

        from bernstein.core.security.agent_card_signer import canonicalize_jcs

        _, public_pem = keypair
        doc = _feed_document(keypair)
        doc["feed"]["releases"][2]["version"] = "9.9.9"
        doc["feed_sha256"] = hashlib.sha256(canonicalize_jcs(doc["feed"])).hexdigest()
        result = verify_release_feed_document(doc, trust_root_pem=public_pem)
        assert not result.ok
        assert "signature does not verify" in result.reason

    def test_attacker_key_swap_is_refused(self, keypair: tuple[bytes, str]) -> None:
        """A self-consistent feed signed by a foreign key is not authorship."""
        _, trusted_public = keypair
        rogue_private, rogue_public = generate_ed25519_keypair()
        rogue_doc = _feed_document((rogue_private, rogue_public.decode("ascii")))
        result = verify_release_feed_document(rogue_doc, trust_root_pem=trusted_public)
        assert not result.ok
        assert "trust root" in result.reason

    def test_missing_signature_is_refused(self, keypair: tuple[bytes, str]) -> None:
        _, public_pem = keypair
        doc = _feed_document(keypair)
        doc.pop("signature")
        assert not verify_release_feed_document(doc, trust_root_pem=public_pem).ok

    def test_no_trust_root_fails_closed(self, keypair: tuple[bytes, str]) -> None:
        result = verify_release_feed_document(_feed_document(keypair), trust_root_pem="")
        assert not result.ok
        assert "trust root" in result.reason


# ---------------------------------------------------------------------------
# Opt-in / air-gap gating: zero network calls by default
# ---------------------------------------------------------------------------


class TestCheckPermission:
    def test_default_is_no_check(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ENV_UPDATE_CHECK, raising=False)
        monkeypatch.delenv("BERNSTEIN_PROFILE_MODE", raising=False)
        permission = resolve_check_permission()
        assert not permission.allowed
        assert ENV_UPDATE_CHECK in permission.reason

    def test_env_opt_in_allows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BERNSTEIN_PROFILE_MODE", raising=False)
        monkeypatch.delenv("BERNSTEIN_NETWORK_POLICY", raising=False)
        monkeypatch.setenv(ENV_UPDATE_CHECK, "1")
        assert resolve_check_permission().allowed

    def test_explicit_request_allows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ENV_UPDATE_CHECK, raising=False)
        monkeypatch.delenv("BERNSTEIN_PROFILE_MODE", raising=False)
        monkeypatch.delenv("BERNSTEIN_NETWORK_POLICY", raising=False)
        assert resolve_check_permission(explicit_request=True).allowed

    def test_airgap_profile_overrides_the_opt_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The declared posture outranks both the env opt-in and an explicit run."""
        monkeypatch.setenv(ENV_UPDATE_CHECK, "1")
        monkeypatch.setenv("BERNSTEIN_PROFILE_MODE", "airgap")
        permission = resolve_check_permission(explicit_request=True)
        assert not permission.allowed
        assert permission.offline_profile

    def test_deny_all_policy_blocks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BERNSTEIN_PROFILE_MODE", raising=False)
        monkeypatch.setenv("BERNSTEIN_NETWORK_POLICY", "none")
        permission = resolve_check_permission(explicit_request=True)
        assert not permission.allowed
        assert permission.offline_profile


class TestFetcherHonoursEgressPolicy:
    def test_airgap_denies_at_the_socket_layer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The off-switch holds even when a caller reaches the fetcher directly."""
        import urllib.request

        from bernstein.core.security.network_policy import NetworkPolicyDenied

        def _explode(*_args: object, **_kwargs: object) -> None:
            pytest.fail("update check opened a socket under the air-gap profile")

        monkeypatch.setattr(urllib.request, "urlopen", _explode)
        monkeypatch.setenv("BERNSTEIN_PROFILE_MODE", "airgap")
        monkeypatch.delenv("BERNSTEIN_NETWORK_POLICY", raising=False)
        with pytest.raises(NetworkPolicyDenied):
            fetch_release_feed("https://example.invalid/release-feed.json")

    def test_non_https_refused_before_any_policy_work(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import urllib.request

        def _explode(*_args: object, **_kwargs: object) -> None:
            pytest.fail("update check opened a socket for a non-https URL")

        monkeypatch.setattr(urllib.request, "urlopen", _explode)
        with pytest.raises(ReleaseFeedError, match="https"):
            fetch_release_feed("http://example.invalid/release-feed.json")


# ---------------------------------------------------------------------------
# Surface delta from the signed tag
# ---------------------------------------------------------------------------


class TestSurfaceDelta:
    def test_counts_come_from_the_signed_tag(self, keypair: tuple[bytes, str]) -> None:
        feed = parse_release_feed(_feed_document(keypair)["feed"])
        delta = classify_surface_delta(feed.releases, installed_version="3.9.0", candidate_version="3.12.0")
        assert delta.total == 3
        assert delta.counts[SURFACE_VERIFICATION] == 1
        assert delta.counts[SURFACE_SECURITY] == 1
        assert delta.counts[SURFACE_FEATURE] == 1
        assert delta.highest == SURFACE_SECURITY

    def test_partial_gap(self, keypair: tuple[bytes, str]) -> None:
        feed = parse_release_feed(_feed_document(keypair)["feed"])
        delta = classify_surface_delta(feed.releases, installed_version="3.10.0", candidate_version="3.11.0")
        assert delta.versions == ("3.11.0",)
        assert delta.highest == SURFACE_VERIFICATION

    def test_no_gap_is_empty(self, keypair: tuple[bytes, str]) -> None:
        feed = parse_release_feed(_feed_document(keypair)["feed"])
        delta = classify_surface_delta(feed.releases, installed_version="3.12.0", candidate_version="3.12.0")
        assert delta.total == 0
        assert delta.highest is None

    def test_yanked_release_is_never_counted(self, keypair: tuple[bytes, str]) -> None:
        entries = [_entry("3.10.0", SURFACE_FEATURE), _entry("3.11.0", SURFACE_SECURITY, yanked=True)]
        feed = parse_release_feed(_feed_document(keypair, entries)["feed"])
        delta = classify_surface_delta(feed.releases, installed_version="3.9.0", candidate_version="3.11.0")
        assert delta.counts[SURFACE_SECURITY] == 0
        assert select_candidate(feed, installed_version="3.9.0") is not None
        assert select_candidate(feed, installed_version="3.9.0").version == "3.10.0"  # type: ignore[union-attr]


class TestCandidateSelection:
    def test_newest_wins(self, keypair: tuple[bytes, str]) -> None:
        feed = parse_release_feed(_feed_document(keypair)["feed"])
        candidate = select_candidate(feed, installed_version="3.9.0")
        assert candidate is not None
        assert candidate.version == "3.12.0"

    def test_pin_caps_the_selection(self, keypair: tuple[bytes, str]) -> None:
        feed = parse_release_feed(_feed_document(keypair)["feed"])
        candidate = select_candidate(feed, installed_version="3.9.0", pinned_version="3.10.0")
        assert candidate is not None
        assert candidate.version == "3.10.0"

    def test_up_to_date_yields_none(self, keypair: tuple[bytes, str]) -> None:
        feed = parse_release_feed(_feed_document(keypair)["feed"])
        assert select_candidate(feed, installed_version="4.0.0") is None

    def test_version_comparison(self) -> None:
        assert compare_versions("3.9.0", "3.10.0") == -1
        assert compare_versions("3.10.0", "3.9.0") == 1
        assert compare_versions("3.9.0", "3.9.0") == 0
        # Unparseable sorts below anything real, so it can never be recommended.
        assert compare_versions("not-a-version", "3.9.0") == -1


# ---------------------------------------------------------------------------
# The advisory: artefact-as-proof
# ---------------------------------------------------------------------------


class TestAdvisorySealing:
    def _sealed(self, keypair: tuple[bytes, str], **kwargs: Any) -> dict[str, Any]:
        private_pem, public_pem = keypair
        feed = parse_release_feed(_feed_document(keypair)["feed"])
        advisory = build_update_advisory(
            feed,
            installed_version=kwargs.pop("installed_version", "3.9.0"),
            chain_anchor=kwargs.pop("chain_anchor", "deadbeef" * 8),
            trust_root_pem=public_pem,
            **kwargs,
        )
        return seal_advisory(advisory, private_key_pem=private_pem, public_key_pem=public_pem)

    def test_round_trip_verifies_offline(self, keypair: tuple[bytes, str]) -> None:
        result = verify_advisory_document(self._sealed(keypair))
        assert result.ok
        assert result.signature_ok
        assert result.content_hash_ok
        assert result.chain_anchor == "deadbeef" * 8
        assert result.advisory is not None
        assert result.advisory["candidate_version"] == "3.12.0"
        assert result.advisory["provenance_verified"] is True

    def test_stripping_the_signature_fails_verification(self, keypair: tuple[bytes, str]) -> None:
        doc = self._sealed(keypair)
        doc.pop("signature")
        result = verify_advisory_document(doc)
        assert not result.ok
        assert "not a receipt" in result.reason

    def test_stripping_the_chain_anchor_fails_verification(self, keypair: tuple[bytes, str]) -> None:
        doc = self._sealed(keypair)
        doc["advisory"]["checked_at_chain_anchor"] = ""
        result = verify_advisory_document(doc)
        assert not result.ok
        assert "chain anchor" in result.reason

    def test_edited_candidate_fails_verification(self, keypair: tuple[bytes, str]) -> None:
        doc = self._sealed(keypair)
        doc["advisory"]["candidate_version"] = "99.0.0"
        result = verify_advisory_document(doc)
        assert not result.ok

    def test_bare_version_diff_is_not_an_advisory(self) -> None:
        """The thing this feature refuses to be: two numbers in a dict."""
        result = verify_advisory_document({"installed": "3.9.0", "latest": "3.12.0"})
        assert not result.ok

    def test_up_to_date_advisory_has_no_candidate(self, keypair: tuple[bytes, str]) -> None:
        doc = self._sealed(keypair, installed_version="4.0.0")
        result = verify_advisory_document(doc)
        assert result.ok
        assert result.advisory is not None
        assert result.advisory["candidate_version"] is None
        assert result.advisory["provenance_verified"] is False

    def test_pin_is_recorded_in_the_advisory(self, keypair: tuple[bytes, str]) -> None:
        doc = self._sealed(keypair, pinned_version="3.10.0")
        assert doc["advisory"]["pinned_version"] == "3.10.0"
        assert doc["advisory"]["candidate_version"] == "3.10.0"

    def test_cache_round_trip(self, keypair: tuple[bytes, str], tmp_path: Path) -> None:
        doc = self._sealed(keypair)
        store_cached_advisory(doc, home=tmp_path)
        loaded = load_cached_advisory(tmp_path)
        assert loaded is not None
        assert verify_advisory_document(loaded).ok

    def test_cache_freshness_window(self, keypair: tuple[bytes, str]) -> None:
        fresh = self._sealed(keypair)
        assert cache_is_fresh(fresh)
        stale = self._sealed(keypair)
        stale["advisory"]["checked_at"] = "2020-01-01T00:00:00Z"
        assert not cache_is_fresh(stale)

    def test_missing_cache_returns_none(self, tmp_path: Path) -> None:
        assert load_cached_advisory(tmp_path) is None


# ---------------------------------------------------------------------------
# Trust root resolution
# ---------------------------------------------------------------------------


class TestTrustRoot:
    def test_env_override(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        pem = tmp_path / "root.pem"
        pem.write_text("-----BEGIN PUBLIC KEY-----\nabc\n-----END PUBLIC KEY-----\n")
        monkeypatch.setenv(ENV_TRUST_ROOT, str(pem))
        loaded, source = load_trust_root(home=tmp_path)
        assert "abc" in loaded
        assert source == f"${ENV_TRUST_ROOT}"

    def test_home_default(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ENV_TRUST_ROOT, raising=False)
        target = tmp_path / ".bernstein" / "release-trust-root.pem"
        target.parent.mkdir(parents=True)
        target.write_text("pem-body\n")
        loaded, _source = load_trust_root(home=tmp_path)
        assert loaded.strip() == "pem-body"

    def test_absent_returns_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ENV_TRUST_ROOT, raising=False)
        loaded, source = load_trust_root(home=tmp_path)
        assert loaded == ""
        assert source == "none"


# ---------------------------------------------------------------------------
# Version pin
# ---------------------------------------------------------------------------


class TestVersionPin:
    def test_round_trip(self, keypair: tuple[bytes, str], tmp_path: Path) -> None:
        private_pem, public_pem = keypair
        write_version_pin(
            VersionPin(version="3.9.0", pinned_at="2026-07-01T00:00:00Z", reason="validated build"),
            private_key_pem=private_pem,
            public_key_pem=public_pem,
            home=tmp_path,
        )
        pin, reason = read_version_pin(tmp_path)
        assert pin is not None
        assert pin.version == "3.9.0"
        assert "verified" in reason

    def test_tampered_pin_is_refused(self, keypair: tuple[bytes, str], tmp_path: Path) -> None:
        private_pem, public_pem = keypair
        path = write_version_pin(
            VersionPin(version="3.9.0", pinned_at="2026-07-01T00:00:00Z"),
            private_key_pem=private_pem,
            public_key_pem=public_pem,
            home=tmp_path,
        )
        doc = json.loads(path.read_text())
        doc["pin"]["version"] = "9.9.9"
        path.write_text(json.dumps(doc))
        pin, reason = read_version_pin(tmp_path)
        assert pin is None
        assert "content hash" in reason

    def test_absent_pin(self, tmp_path: Path) -> None:
        pin, reason = read_version_pin(tmp_path)
        assert pin is None
        assert "no version pin" in reason

    def test_pin_blocks_higher_targets_only(self) -> None:
        pin = VersionPin(version="3.10.0", pinned_at="2026-07-01T00:00:00Z")
        assert pin_blocks(pin, "3.11.0")
        assert not pin_blocks(pin, "3.10.0")
        assert not pin_blocks(pin, "3.9.0")
        assert not pin_blocks(None, "9.9.9")


# ---------------------------------------------------------------------------
# Pre-install wheel verification
# ---------------------------------------------------------------------------


class TestWheelVerification:
    def _wheel(self, tmp_path: Path) -> Path:
        wheel = tmp_path / "bernstein-3.12.0-py3-none-any.whl"
        wheel.write_bytes(_WHEEL_BODY)
        return wheel

    def test_matching_hash_passes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        wheel = self._wheel(tmp_path)
        monkeypatch.setattr(
            "bernstein.core.distribution.sigstore_attestation_verify.shutil.which",
            lambda _name: None,
        )
        verdict = verify_wheel_against_advisory(
            wheel,
            advisory={"candidate_wheel_sha256": sha256_file(wheel)},
        )
        assert verdict.ok
        assert verdict.attestation_ok is None

    def test_mismatch_aborts(self, tmp_path: Path) -> None:
        wheel = self._wheel(tmp_path)
        verdict = verify_wheel_against_advisory(wheel, advisory={"candidate_wheel_sha256": _WHEEL_SHA256})
        assert not verdict.ok
        assert "hash mismatch" in verdict.reason

    def test_missing_expected_hash_aborts(self, tmp_path: Path) -> None:
        verdict = verify_wheel_against_advisory(self._wheel(tmp_path), advisory={})
        assert not verdict.ok
        assert "names no candidate wheel hash" in verdict.reason

    def test_missing_file_aborts(self, tmp_path: Path) -> None:
        verdict = verify_wheel_against_advisory(
            tmp_path / "absent.whl",
            advisory={"candidate_wheel_sha256": _WHEEL_SHA256},
        )
        assert not verdict.ok

    def test_require_attestation_refuses_a_skip(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        wheel = self._wheel(tmp_path)
        monkeypatch.setattr(
            "bernstein.core.distribution.sigstore_attestation_verify.shutil.which",
            lambda _name: None,
        )
        verdict = verify_wheel_against_advisory(
            wheel,
            advisory={"candidate_wheel_sha256": sha256_file(wheel)},
            require_attestation=True,
        )
        assert not verdict.ok
        assert verdict.attestation_ok is False


# ---------------------------------------------------------------------------
# Install receipts
# ---------------------------------------------------------------------------


class TestInstallReceipts:
    def _receipt(self, direction: str, *, frm: str, to: str, when: str) -> dict[str, Any]:
        return build_install_receipt(
            from_version=frm,
            to_version=to,
            wheel_sha256=_WHEEL_SHA256,
            provenance_key_fingerprint="sha256:aaa",
            advisory_sha256_value="bbb",
            direction=direction,
            chain_anchor="cafe" * 16,
            attestation_ok=True,
            generated_at=when,
        )

    def test_receipt_is_deterministic(self) -> None:
        first = self._receipt("install", frm="3.9.0", to="3.12.0", when="2026-07-01T00:00:00Z")
        second = self._receipt("install", frm="3.9.0", to="3.12.0", when="2026-07-01T00:00:00Z")
        assert receipt_sha256(first) == receipt_sha256(second)

    def test_store_and_read_back(self, tmp_path: Path) -> None:
        receipt = self._receipt("install", frm="3.9.0", to="3.12.0", when="2026-07-01T00:00:00Z")
        store_receipt(receipt, home=tmp_path)
        assert previous_receipted_version(tmp_path) == "3.9.0"

    def test_tampered_receipt_is_dropped(self, tmp_path: Path) -> None:
        receipt = self._receipt("install", frm="3.9.0", to="3.12.0", when="2026-07-01T00:00:00Z")
        path = store_receipt(receipt, home=tmp_path)
        doc = json.loads(path.read_text())
        doc["receipt"]["from_version"] = "0.0.1"
        path.write_text(json.dumps(doc))
        assert previous_receipted_version(tmp_path) is None

    def test_newest_install_wins(self, tmp_path: Path) -> None:
        store_receipt(self._receipt("install", frm="3.8.0", to="3.9.0", when="2026-06-01T00:00:00Z"), home=tmp_path)
        store_receipt(self._receipt("install", frm="3.9.0", to="3.12.0", when="2026-07-01T00:00:00Z"), home=tmp_path)
        assert previous_receipted_version(tmp_path) == "3.9.0"

    def test_no_receipts_yields_none(self, tmp_path: Path) -> None:
        assert previous_receipted_version(tmp_path) is None


# ---------------------------------------------------------------------------
# Audit-chain anchoring
# ---------------------------------------------------------------------------


class TestChainAnchoring:
    def test_advisory_event_lands_in_the_chain(self, tmp_path: Path, keypair: tuple[bytes, str]) -> None:
        from bernstein.core.security.audit_chain import (
            EVENT_UPDATE_ADVISORY,
            AuditChainStore,
            record_update_advisory,
        )

        _, public_pem = keypair
        feed = parse_release_feed(_feed_document(keypair)["feed"])
        advisory = build_update_advisory(
            feed,
            installed_version="3.9.0",
            chain_anchor="anchor",
            trust_root_pem=public_pem,
        )
        chain = AuditChainStore(tmp_path / "audit", key=b"k" * 32)
        event = record_update_advisory(
            chain=chain,
            advisory_sha256="sha-advisory",
            installed_version=advisory.installed_version,
            candidate_version=advisory.candidate_version,
            candidate_wheel_sha256=advisory.candidate_wheel_sha256,
            provenance_verified=advisory.provenance_verified,
            surface_delta=advisory.surface_delta.to_dict(),
            feed_sha256=advisory.feed_sha256,
            trust_root_fingerprint=advisory.trust_root_fingerprint,
        )
        assert event.event_type == EVENT_UPDATE_ADVISORY
        rows = chain.query(event_type=EVENT_UPDATE_ADVISORY)
        assert len(rows) == 1
        assert rows[0].details["candidate_version"] == "3.12.0"
        assert rows[0].details["provenance_verified"] is True
        assert "prev_chain_digest" in rows[0].details
        assert chain.verify()[0]

    def test_install_receipt_event_lands_in_the_chain(self, tmp_path: Path) -> None:
        from bernstein.core.security.audit_chain import (
            EVENT_SELF_UPDATE,
            AuditChainStore,
            record_self_update_receipt,
        )

        chain = AuditChainStore(tmp_path / "audit", key=b"k" * 32)
        event = record_self_update_receipt(
            chain=chain,
            receipt_sha256="sha-receipt",
            direction="install",
            from_version="3.9.0",
            to_version="3.12.0",
            wheel_sha256=_WHEEL_SHA256,
            provenance_key_fingerprint="sha256:aaa",
            advisory_sha256="sha-advisory",
            attestation_verified=None,
        )
        assert event.event_type == EVENT_SELF_UPDATE
        rows = chain.query(event_type=EVENT_SELF_UPDATE)
        assert rows[0].details["direction"] == "install"
        assert rows[0].details["to_version"] == "3.12.0"
        assert "prev_chain_digest" in rows[0].details
        assert chain.verify()[0]

    def test_rollback_receipt_is_distinguishable(self, tmp_path: Path) -> None:
        from bernstein.core.security.audit_chain import (
            EVENT_SELF_UPDATE,
            AuditChainStore,
            record_self_update_receipt,
        )

        chain = AuditChainStore(tmp_path / "audit", key=b"k" * 32)
        record_self_update_receipt(
            chain=chain,
            receipt_sha256="sha-1",
            direction="install",
            from_version="3.9.0",
            to_version="3.12.0",
            wheel_sha256=_WHEEL_SHA256,
            provenance_key_fingerprint="sha256:aaa",
            advisory_sha256="a",
            attestation_verified=True,
        )
        record_self_update_receipt(
            chain=chain,
            receipt_sha256="sha-2",
            direction="rollback",
            from_version="3.12.0",
            to_version="3.9.0",
            wheel_sha256=_WHEEL_SHA256,
            provenance_key_fingerprint="sha256:aaa",
            advisory_sha256="",
            attestation_verified=None,
        )
        directions = [row.details["direction"] for row in chain.query(event_type=EVENT_SELF_UPDATE)]
        assert directions == ["install", "rollback"]
        assert chain.verify()[0]


# ---------------------------------------------------------------------------
# End-to-end: hermetic signed feed on disk
# ---------------------------------------------------------------------------


class TestHermeticFeedOnDisk:
    def test_mirrored_feed_drives_a_verified_advisory(
        self,
        tmp_path: Path,
        keypair: tuple[bytes, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The air-gap path: a signed feed file, no network, a sealed advisory."""
        import urllib.request

        from bernstein.core.distribution.update_advisory import load_release_feed

        def _explode(*_args: object, **_kwargs: object) -> None:
            pytest.fail("a mirrored-feed check must not open a socket")

        monkeypatch.setattr(urllib.request, "urlopen", _explode)
        monkeypatch.setenv("BERNSTEIN_PROFILE_MODE", "airgap")

        private_pem, public_pem = keypair
        feed_file = tmp_path / "release-feed.json"
        feed_file.write_text(json.dumps(_feed_document(keypair)))
        monkeypatch.setenv(ENV_RELEASE_FEED, str(feed_file))

        document = load_release_feed(feed_file)
        verification = verify_release_feed_document(document, trust_root_pem=public_pem)
        assert verification.ok
        assert verification.feed is not None

        advisory = build_update_advisory(
            verification.feed,
            installed_version="3.9.0",
            chain_anchor="anchor" * 4,
            trust_root_pem=public_pem,
            offline_profile=True,
        )
        sealed = seal_advisory(advisory, private_key_pem=private_pem, public_key_pem=public_pem)
        assert verify_advisory_document(sealed).ok
        assert sealed["advisory"]["offline_profile"] is True
        assert sealed["advisory"]["candidate_version"] == "3.12.0"

    def test_tampered_mirrored_feed_yields_no_candidate(
        self,
        tmp_path: Path,
        keypair: tuple[bytes, str],
    ) -> None:
        from bernstein.core.distribution.update_advisory import load_release_feed

        _, public_pem = keypair
        document = _feed_document(keypair)
        document["feed"]["releases"][2]["wheel_sha256"] = "0" * 64
        feed_file = tmp_path / "release-feed.json"
        feed_file.write_text(json.dumps(document))
        verification = verify_release_feed_document(load_release_feed(feed_file), trust_root_pem=public_pem)
        assert not verification.ok
        assert verification.feed is None

    def test_missing_feed_file_raises(self, tmp_path: Path) -> None:
        from bernstein.core.distribution.update_advisory import load_release_feed

        with pytest.raises(ReleaseFeedError, match="cannot read"):
            load_release_feed(tmp_path / "nope.json")

    def test_non_json_feed_file_raises(self, tmp_path: Path) -> None:
        from bernstein.core.distribution.update_advisory import load_release_feed

        path = tmp_path / "feed.json"
        path.write_text("not json")
        with pytest.raises(ReleaseFeedError, match="not valid JSON"):
            load_release_feed(path)


def test_advisory_timestamp_is_utc(keypair: tuple[bytes, str]) -> None:
    _, public_pem = keypair
    feed = parse_release_feed(_feed_document(keypair)["feed"])
    advisory = build_update_advisory(
        feed,
        installed_version="3.9.0",
        chain_anchor="x",
        trust_root_pem=public_pem,
    )
    parsed = dt.datetime.fromisoformat(advisory.checked_at.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
