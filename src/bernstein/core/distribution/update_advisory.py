"""Provenance-verified release update advisory (issue #2942).

An operator learns that a newer release exists the same way they learn
anything else in this project: from a signed receipt they can re-check
offline, not from a version string a registry printed at them.

The primary artefact is the **update advisory** -- a canonical document
binding ``{installed_version, candidate_version, candidate_wheel_sha256,
provenance_verified, surface_delta, checked_at_chain_anchor}``, sealed with a
detached Ed25519 JWS over its JCS-canonical bytes and anchored to the HMAC
audit chain head that was current when the check ran. ``bernstein self
check-update --verify <file>`` recomputes the whole thing without a network
call. Strip the signature or the chain anchor and
:func:`verify_advisory_document` refuses it: what is left is a version diff,
which is exactly the thing this module exists not to be.

Three properties are load-bearing and are enforced here in ``core`` rather
than in the CLI, so no caller can opt out of them by calling a lower layer:

1. **Offline-first and opt-in.** :func:`resolve_check_permission` is the only
   gate to a remote fetch. A check runs on an explicit operator request or
   with ``BERNSTEIN_UPDATE_CHECK=1``; nothing else reaches the network. Under
   the air-gap / sovereign profile the remote path is refused outright even
   when the opt-in is set, and the fetcher additionally re-checks the live
   :class:`~bernstein.core.security.network_policy.NetworkPolicy` immediately
   before opening a socket -- so a deny-all posture stops the request at the
   egress layer even if a caller reached :func:`fetch_release_feed` directly.

2. **Provenance before recommendation.** A candidate is only ever surfaced
   after its release feed entry verifies against the operator's configured
   trust root. A feed with one flipped byte fails the signature check and the
   candidate is dropped, not shown with a warning. With no trust root
   configured the check fails closed rather than trusting the feed.

3. **Provenance before install.** :func:`verify_wheel_against_advisory`
   recomputes the sha256 of the downloaded wheel and compares it to the
   advisory's provenance-checked hash; a mismatch aborts before pip is
   invoked. Sigstore build-provenance verification (the attestation
   ``publish.yml`` already emits via ``actions/attest-build-provenance``) runs
   as an additional escalating pass through
   :class:`~bernstein.core.distribution.sigstore_attestation_verify.SigstoreAttestationVerifier`.

The surface delta is read from the signed ``surface`` field on each release
entry (``verification`` / ``security`` / ``feature``), never parsed out of
release-note prose, so "you are two releases behind on the verification
surface" is an authenticated claim rather than a heuristic.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, cast

from bernstein.core.security.agent_card_signer import (
    canonicalize_jcs,
    sign_detached_jws_over_canonical,
    verify_detached_jws_over_canonical,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from pathlib import Path

__all__ = [
    "ADVISORY_JWS_TYP",
    "ADVISORY_KIND",
    "DEFAULT_MIN_CHECK_INTERVAL_S",
    "ENV_RELEASE_FEED",
    "ENV_TRUST_ROOT",
    "ENV_UPDATE_CHECK",
    "KNOWN_SURFACES",
    "PIN_JWS_TYP",
    "RELEASE_FEED_JWS_TYP",
    "RELEASE_FEED_SCHEMA_VERSION",
    "SURFACE_FEATURE",
    "SURFACE_SECURITY",
    "SURFACE_VERIFICATION",
    "UPDATE_ADVISORY_SCHEMA_VERSION",
    "AdvisoryVerification",
    "CheckPermission",
    "FeedVerification",
    "ReleaseEntry",
    "ReleaseFeed",
    "ReleaseFeedError",
    "SurfaceDelta",
    "UpdateAdvisory",
    "VersionPin",
    "WheelVerification",
    "advisory_sha256",
    "build_install_receipt",
    "build_release_feed_document",
    "build_update_advisory",
    "cache_is_fresh",
    "cache_path",
    "classify_surface_delta",
    "compare_versions",
    "feed_cache_path",
    "fetch_release_feed",
    "install_identity_pems",
    "load_cached_advisory",
    "load_cached_feed",
    "load_release_feed",
    "load_trust_root",
    "parse_release_feed",
    "pin_blocks",
    "pin_path",
    "previous_receipted_version",
    "read_receipts",
    "read_version_pin",
    "receipt_sha256",
    "receipts_dir",
    "resolve_check_permission",
    "seal_advisory",
    "select_candidate",
    "sha256_file",
    "store_cached_advisory",
    "store_cached_feed",
    "store_receipt",
    "trust_root_fingerprint",
    "verify_advisory_document",
    "verify_release_feed_document",
    "verify_wheel_against_advisory",
    "write_version_pin",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Schema version stamped into every advisory preimage.
UPDATE_ADVISORY_SCHEMA_VERSION: Final[int] = 1

#: Schema version stamped into the signed release feed.
RELEASE_FEED_SCHEMA_VERSION: Final[int] = 1

#: Distribution name this module reasons about.
PACKAGE_NAME: Final[str] = "bernstein"

#: ``kind`` discriminator on the advisory preimage; also the audit event type.
ADVISORY_KIND: Final[str] = "update.advisory"

#: ``kind`` discriminator on the release feed preimage.
RELEASE_FEED_KIND: Final[str] = "bernstein.release_feed"

#: JWS ``typ`` binding an advisory signature to the advisory surface, so a
#: signature minted elsewhere cannot be replayed as an advisory.
ADVISORY_JWS_TYP: Final[str] = "bernstein-update-advisory+jws"

#: JWS ``typ`` for the signed release feed.
RELEASE_FEED_JWS_TYP: Final[str] = "bernstein-release-feed+jws"

#: JWS ``typ`` for a signed version pin.
PIN_JWS_TYP: Final[str] = "bernstein-version-pin+jws"

#: Opt-in switch for a background/remote update check. Unset means no check.
ENV_UPDATE_CHECK: Final[str] = "BERNSTEIN_UPDATE_CHECK"

#: Release-feed location: a local path (air-gap mirror) or an https URL.
ENV_RELEASE_FEED: Final[str] = "BERNSTEIN_RELEASE_FEED"

#: Path to the SPKI PEM of the release-feed signing identity (the trust root).
ENV_TRUST_ROOT: Final[str] = "BERNSTEIN_RELEASE_TRUST_ROOT"

#: Minimum seconds between two remote checks. The cached advisory is served
#: in between, so an opted-in operator still makes at most one call a day.
DEFAULT_MIN_CHECK_INTERVAL_S: Final[int] = 86_400

#: Release touched the verification surface (audit chain, lineage, replay).
SURFACE_VERIFICATION: Final[str] = "verification"

#: Release carried a security fix.
SURFACE_SECURITY: Final[str] = "security"

#: Ordinary feature / fix release.
SURFACE_FEATURE: Final[str] = "feature"

#: Every surface tag a signed feed entry may carry.
KNOWN_SURFACES: Final[frozenset[str]] = frozenset(
    {SURFACE_VERIFICATION, SURFACE_SECURITY, SURFACE_FEATURE},
)

#: Severity order used to pick the headline surface of a multi-release gap.
_SURFACE_RANK: Final[dict[str, int]] = {
    SURFACE_FEATURE: 0,
    SURFACE_VERIFICATION: 1,
    SURFACE_SECURITY: 2,
}

_CACHE_RELPATH: Final[tuple[str, ...]] = (".bernstein", "update-advisory.json")
_PIN_RELPATH: Final[tuple[str, ...]] = (".bernstein", "version-pin.json")

_HEX64_LEN: Final[int] = 64


class ReleaseFeedError(ValueError):
    """Raised when a release feed document is malformed.

    Distinct from *unverified*: a feed that parses but fails its signature is
    never raised, it is reported as ``ok=False`` so the caller refuses the
    candidate rather than crashing.
    """


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------


def _canonical(value: Any) -> bytes:
    """JCS-canonical bytes of *value* (the signing / hashing preimage)."""
    return canonicalize_jcs(value)


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _utc_now_iso() -> str:
    return _dt.datetime.now(tz=_dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path, *, chunk_size: int = 1 << 20) -> str:
    """Return the lowercase hex sha256 of the file at *path*."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _normalised_pem(pem: str) -> str:
    """Collapse line-ending and trailing-whitespace differences in a PEM."""
    return "\n".join(line.strip() for line in pem.strip().splitlines() if line.strip())


def trust_root_fingerprint(public_key_pem: str) -> str:
    """``sha256:`` fingerprint of a normalised SPKI PEM.

    Stamped into the advisory so a reader can tell which signing identity the
    recommendation was anchored on without carrying the key itself.
    """
    return f"sha256:{_sha256_hex(_normalised_pem(public_key_pem).encode('utf-8'))}"


def compare_versions(left: str, right: str) -> int:
    """Return -1/0/1 comparing two PEP 440 version strings.

    Uses ``packaging.version`` (already a resolved dependency and the same
    comparator :mod:`bernstein.adapters.advisories` uses for security floors).
    An unparseable version sorts *below* every parseable one rather than
    raising, so a malformed feed entry can never be recommended over a real
    release.
    """
    from packaging.version import InvalidVersion, Version

    def _parse(raw: str) -> Version | None:
        try:
            return Version(raw)
        except InvalidVersion:
            return None

    lv, rv = _parse(left), _parse(right)
    if lv is None and rv is None:
        return 0
    if lv is None:
        return -1
    if rv is None:
        return 1
    if lv < rv:
        return -1
    return 1 if lv > rv else 0


# ---------------------------------------------------------------------------
# Release feed
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReleaseEntry:
    """One release as published in the signed feed.

    Attributes:
        version: PEP 440 version string of the release.
        wheel_name: Wheel filename the operator would install.
        wheel_sha256: Lowercase hex sha256 of that wheel. This is the value
            the pre-install check pins against.
        surface: ``verification`` / ``security`` / ``feature`` -- the signed
            classification the advisory reads instead of parsing prose.
        released_at: RFC 3339 timestamp of publication.
        yanked: True when the release was withdrawn; yanked entries are never
            recommended.
    """

    version: str
    wheel_name: str
    wheel_sha256: str
    surface: str
    released_at: str
    yanked: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical, JSON-serialisable entry document."""
        return {
            "version": self.version,
            "wheel_name": self.wheel_name,
            "wheel_sha256": self.wheel_sha256,
            "surface": self.surface,
            "released_at": self.released_at,
            "yanked": self.yanked,
        }


@dataclass(frozen=True, slots=True)
class ReleaseFeed:
    """A parsed, not-yet-trusted release feed body.

    Parsing and verification are deliberately separate: the body is inert
    data until :func:`verify_release_feed_document` says the signature checks
    out against the operator's trust root.
    """

    schema_version: int
    kind: str
    package: str
    generated_at: str
    releases: tuple[ReleaseEntry, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical, JSON-serialisable feed body."""
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "package": self.package,
            "generated_at": self.generated_at,
            "releases": [entry.to_dict() for entry in self.releases],
        }

    def body_sha256(self) -> str:
        """Content hash of the canonical feed body."""
        return _sha256_hex(_canonical(self.to_dict()))

    def entry_for(self, version: str) -> ReleaseEntry | None:
        """Return the entry for *version*, or ``None``."""
        for entry in self.releases:
            if entry.version == version:
                return entry
        return None


def _require_str(source: Mapping[str, Any], key: str, *, where: str) -> str:
    value: Any = source.get(key)
    if not isinstance(value, str) or not value:
        raise ReleaseFeedError(f"{where}: field {key!r} must be a non-empty string")
    return value


def _parse_entry(raw: Any, *, index: int) -> ReleaseEntry:
    where = f"release[{index}]"
    if not isinstance(raw, dict):
        raise ReleaseFeedError(f"{where}: entry must be an object")
    entry = cast("dict[str, Any]", raw)
    wheel_sha256 = _require_str(entry, "wheel_sha256", where=where).lower()
    if len(wheel_sha256) != _HEX64_LEN or any(ch not in "0123456789abcdef" for ch in wheel_sha256):
        raise ReleaseFeedError(f"{where}: wheel_sha256 must be 64 lowercase hex characters")
    surface = _require_str(entry, "surface", where=where)
    if surface not in KNOWN_SURFACES:
        raise ReleaseFeedError(f"{where}: surface {surface!r} is not one of {sorted(KNOWN_SURFACES)}")
    yanked: Any = entry.get("yanked", False)
    if not isinstance(yanked, bool):
        raise ReleaseFeedError(f"{where}: yanked must be a boolean")
    return ReleaseEntry(
        version=_require_str(entry, "version", where=where),
        wheel_name=_require_str(entry, "wheel_name", where=where),
        wheel_sha256=wheel_sha256,
        surface=surface,
        released_at=_require_str(entry, "released_at", where=where),
        yanked=yanked,
    )


def parse_release_feed(body: Any) -> ReleaseFeed:
    """Parse a release feed *body* into a :class:`ReleaseFeed`.

    Raises:
        ReleaseFeedError: The document is not a well-formed feed body. A
            structurally invalid feed is refused here, before any signature
            work, so malformed input can never reach the candidate selector.
    """
    if not isinstance(body, dict):
        raise ReleaseFeedError("release feed body must be an object")
    document = cast("dict[str, Any]", body)
    schema_version: Any = document.get("schema_version")
    if schema_version != RELEASE_FEED_SCHEMA_VERSION:
        raise ReleaseFeedError(
            f"unsupported release feed schema_version {schema_version!r}; expected {RELEASE_FEED_SCHEMA_VERSION}",
        )
    kind = _require_str(document, "kind", where="feed")
    if kind != RELEASE_FEED_KIND:
        raise ReleaseFeedError(f"release feed kind {kind!r} is not {RELEASE_FEED_KIND!r}")
    raw_releases: Any = document.get("releases")
    if not isinstance(raw_releases, list):
        raise ReleaseFeedError("release feed must carry a 'releases' array")
    entries = tuple(_parse_entry(raw, index=i) for i, raw in enumerate(cast("list[Any]", raw_releases)))
    return ReleaseFeed(
        schema_version=RELEASE_FEED_SCHEMA_VERSION,
        kind=kind,
        package=_require_str(document, "package", where="feed"),
        generated_at=_require_str(document, "generated_at", where="feed"),
        releases=entries,
    )


def build_release_feed_document(
    releases: Sequence[ReleaseEntry],
    *,
    private_key_pem: bytes,
    public_key_pem: str,
    generated_at: str | None = None,
    package: str = PACKAGE_NAME,
    kid: str = "release-feed",
) -> dict[str, Any]:
    """Seal *releases* into a signed release-feed document.

    Deterministic: with a fixed ``generated_at`` the canonical body bytes --
    and therefore ``feed_sha256`` -- are byte-identical on any host, so the
    release pipeline and an operator mirroring the feed can prove they hold
    the same document by hash alone.
    """
    feed = ReleaseFeed(
        schema_version=RELEASE_FEED_SCHEMA_VERSION,
        kind=RELEASE_FEED_KIND,
        package=package,
        generated_at=generated_at or _utc_now_iso(),
        releases=tuple(releases),
    )
    body = feed.to_dict()
    canonical = _canonical(body)
    return {
        "feed": body,
        "feed_sha256": _sha256_hex(canonical),
        "signature": sign_detached_jws_over_canonical(
            canonical,
            private_key_pem,
            typ=RELEASE_FEED_JWS_TYP,
            kid=kid,
        ),
        "public_key": public_key_pem,
    }


@dataclass(frozen=True, slots=True)
class FeedVerification:
    """Outcome of checking a release-feed document against a trust root.

    ``ok`` is True only when the body parses, its recorded content hash
    matches the recomputed one, the embedded key equals the trust root, and
    the detached JWS verifies. Anything else is a refusal with a reason -- the
    caller never gets a partially-trusted feed.
    """

    ok: bool
    reason: str
    feed: ReleaseFeed | None = None

    def __bool__(self) -> bool:
        return self.ok


def verify_release_feed_document(doc: Any, *, trust_root_pem: str) -> FeedVerification:
    """Verify a signed release-feed document offline against *trust_root_pem*.

    The embedded ``public_key`` is checked for equality with the trust root
    before the signature is believed. A document that verifies only against
    the key it carries proves self-consistency, not authorship: anyone who can
    rewrite the feed can also mint a fresh keypair for it. This mirrors the
    key-anchoring the sovereign posture attestation performs.

    Never raises on hostile input -- a malformed document is a refusal.
    """
    if not isinstance(doc, dict):
        return FeedVerification(ok=False, reason="release feed document must be an object")
    envelope = cast("dict[str, Any]", doc)
    if not trust_root_pem.strip():
        return FeedVerification(ok=False, reason="no release trust root configured; refusing to trust the feed")

    signature: Any = envelope.get("signature")
    if not isinstance(signature, str) or not signature:
        return FeedVerification(ok=False, reason="release feed carries no signature")
    embedded_key: Any = envelope.get("public_key")
    if not isinstance(embedded_key, str) or not embedded_key.strip():
        return FeedVerification(ok=False, reason="release feed carries no public key")
    if _normalised_pem(embedded_key) != _normalised_pem(trust_root_pem):
        return FeedVerification(
            ok=False,
            reason="release feed signing key does not match the configured trust root",
        )

    try:
        feed = parse_release_feed(envelope.get("feed"))
    except ReleaseFeedError as exc:
        return FeedVerification(ok=False, reason=f"malformed release feed: {exc}")

    canonical = _canonical(feed.to_dict())
    recorded_hash: Any = envelope.get("feed_sha256")
    recomputed = _sha256_hex(canonical)
    if not isinstance(recorded_hash, str) or recorded_hash != recomputed:
        return FeedVerification(
            ok=False,
            reason="release feed content hash does not match its body (tampered or truncated)",
        )
    if not verify_detached_jws_over_canonical(
        canonical,
        signature,
        _normalised_pem(trust_root_pem).encode("ascii") + b"\n",
        expected_typ=RELEASE_FEED_JWS_TYP,
    ):
        return FeedVerification(ok=False, reason="release feed signature does not verify against the trust root")
    return FeedVerification(
        ok=True,
        reason="release feed signature verified against the configured trust root",
        feed=feed,
    )


def load_trust_root(explicit: Path | None = None, *, home: Path | None = None) -> tuple[str, str]:
    """Resolve the release-feed trust root PEM.

    Resolution order: *explicit* path, then :data:`ENV_TRUST_ROOT`, then
    ``~/.bernstein/release-trust-root.pem``.

    Returns:
        ``(pem, source)``. ``pem`` is empty when no trust root is installed;
        callers must treat that as a refusal, not as "skip verification".
    """
    from pathlib import Path as _Path

    candidates: list[tuple[Path, str]] = []
    if explicit is not None:
        candidates.append((explicit, str(explicit)))
    env_value = os.environ.get(ENV_TRUST_ROOT, "").strip()
    if env_value:
        candidates.append((_Path(env_value).expanduser(), f"${ENV_TRUST_ROOT}"))
    base = home if home is not None else _Path.home()
    candidates.append((base / ".bernstein" / "release-trust-root.pem", "~/.bernstein/release-trust-root.pem"))

    for path, source in candidates:
        try:
            if path.is_file():
                return path.read_text(encoding="utf-8"), source
        except OSError:  # unreadable candidate: fall through to the next one
            continue
    return "", "none"


def load_release_feed(path: Path) -> dict[str, Any]:
    """Read a release-feed document from a local (mirrored) file.

    Raises:
        ReleaseFeedError: The file is missing or is not JSON.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReleaseFeedError(f"cannot read release feed {path}: {exc}") from exc
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReleaseFeedError(f"release feed {path} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ReleaseFeedError(f"release feed {path} must contain a JSON object")
    return cast("dict[str, Any]", parsed)


# ---------------------------------------------------------------------------
# Opt-in / air-gap gating
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CheckPermission:
    """Whether a remote update check may run, and why / why not.

    Attributes:
        allowed: True only when a remote fetch is permitted right now.
        reason: Operator-facing explanation, always set.
        offline_profile: True when the air-gap / sovereign profile is active.
            Under this profile ``allowed`` is always False and the caller must
            fall back to a locally-mirrored feed or report "offline profile".
    """

    allowed: bool
    reason: str
    offline_profile: bool = False

    def __bool__(self) -> bool:
        return self.allowed


def _truthy(raw: str | None) -> bool:
    return (raw or "").strip().lower() in {"1", "true", "yes", "on"}


def resolve_check_permission(*, explicit_request: bool = False) -> CheckPermission:
    """Decide whether this process may make a remote update check.

    The rules, in order:

    1. Air-gap / sovereign profile active -> never, regardless of the opt-in.
       The profile is the operator's declared posture and outranks a stray
       environment variable.
    2. Live egress policy denies the feed host -> never.
    3. Explicit operator request (``bernstein self check-update``) -> yes.
    4. :data:`ENV_UPDATE_CHECK` truthy -> yes.
    5. Otherwise -> no. This is the default: an ordinary run makes zero
       network calls for update purposes.
    """
    from bernstein.core.security.network_policy import is_airgap_profile, policy_from_env

    try:
        airgap = is_airgap_profile()
    except Exception:  # pragma: no cover - defensive: env read must never break a run
        airgap = False
    if airgap:
        return CheckPermission(
            allowed=False,
            reason="air-gap/sovereign profile active - the remote update path is disabled",
            offline_profile=True,
        )
    if not explicit_request and not _truthy(os.environ.get(ENV_UPDATE_CHECK)):
        return CheckPermission(
            allowed=False,
            reason=f"update checks are opt-in; set {ENV_UPDATE_CHECK}=1 or run `bernstein self check-update`",
        )
    policy = policy_from_env()
    if not policy.allow_any and not policy.rules:
        return CheckPermission(
            allowed=False,
            reason="network policy is deny-all - the remote update path is disabled",
            offline_profile=True,
        )
    return CheckPermission(allowed=True, reason="operator opted in to a remote update check")


def fetch_release_feed(url: str, *, timeout_s: float = 10.0) -> dict[str, Any]:
    """Fetch a release-feed document over https.

    The egress policy is re-checked here, immediately before the socket is
    opened, rather than only at the CLI boundary: a deny-all posture stops the
    request even when a caller reaches this function directly.

    Raises:
        ReleaseFeedError: The URL scheme is not https, the fetch failed, or
            the response is not a JSON object.
        NetworkPolicyDenied: The live egress policy forbids the feed host.
    """
    import urllib.request
    from urllib.parse import urlparse

    from bernstein.core.security.network_policy import policy_from_env

    if urlparse(url).scheme != "https":
        raise ReleaseFeedError(f"release feed URL must be https, got {url!r}")
    policy_from_env().check_url(url, source="update-advisory")
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": f"{PACKAGE_NAME}/update-advisory"},
    )
    try:
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            payload = response.read()
    except OSError as exc:
        raise ReleaseFeedError(f"cannot fetch release feed {url}: {exc}") from exc
    try:
        parsed: Any = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ReleaseFeedError(f"release feed {url} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ReleaseFeedError(f"release feed {url} must contain a JSON object")
    return cast("dict[str, Any]", parsed)


# ---------------------------------------------------------------------------
# Surface delta
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SurfaceDelta:
    """How the gap between installed and candidate is classified.

    Every count comes from the signed ``surface`` field on a feed entry, so
    "two releases changed the verification surface" is an authenticated claim
    rather than the result of grepping release-note prose.
    """

    counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(KNOWN_SURFACES, 0))
    versions: tuple[str, ...] = ()

    @property
    def total(self) -> int:
        """Number of releases in the gap."""
        return sum(self.counts.values())

    @property
    def highest(self) -> str | None:
        """The most severe surface present in the gap, or ``None`` if empty."""
        present = [s for s, n in self.counts.items() if n > 0]
        if not present:
            return None
        return max(present, key=lambda s: _SURFACE_RANK.get(s, 0))

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical, JSON-serialisable delta document."""
        return {
            "counts": {surface: int(self.counts.get(surface, 0)) for surface in sorted(KNOWN_SURFACES)},
            "versions": list(self.versions),
            "total": self.total,
            "highest": self.highest,
        }


def classify_surface_delta(
    entries: Iterable[ReleaseEntry],
    *,
    installed_version: str,
    candidate_version: str,
) -> SurfaceDelta:
    """Classify every release in ``(installed, candidate]`` by signed surface."""
    in_gap = [
        entry
        for entry in entries
        if not entry.yanked
        and compare_versions(installed_version, entry.version) < 0
        and compare_versions(entry.version, candidate_version) <= 0
    ]
    in_gap.sort(key=lambda e: e.version)
    counts = dict.fromkeys(KNOWN_SURFACES, 0)
    for entry in in_gap:
        counts[entry.surface] = counts.get(entry.surface, 0) + 1
    return SurfaceDelta(counts=counts, versions=tuple(entry.version for entry in in_gap))


def select_candidate(
    feed: ReleaseFeed,
    *,
    installed_version: str,
    pinned_version: str | None = None,
) -> ReleaseEntry | None:
    """Pick the release the operator should move to, or ``None``.

    A pin caps the selection: with a pin in force the newest release at or
    below the pin is chosen, so an operator who standardised on a version is
    never told to leave it.
    """
    usable = [entry for entry in feed.releases if not entry.yanked]
    if pinned_version is not None:
        usable = [entry for entry in usable if compare_versions(entry.version, pinned_version) <= 0]
    ahead = [entry for entry in usable if compare_versions(installed_version, entry.version) < 0]
    if not ahead:
        return None
    best = ahead[0]
    for entry in ahead[1:]:
        if compare_versions(entry.version, best.version) > 0:
            best = entry
    return best


# ---------------------------------------------------------------------------
# The advisory
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UpdateAdvisory:
    """The canonical advisory preimage.

    This is what gets signed and what gets anchored. ``checked_at_chain_anchor``
    is the audit-chain head that was current when the check ran, which is what
    makes "on date D we checked, found vV, verified its provenance" a
    reconstructable position in the chain rather than a self-asserted date.
    """

    installed_version: str
    candidate_version: str | None
    candidate_wheel_name: str | None
    candidate_wheel_sha256: str | None
    provenance_verified: bool
    provenance_source: str
    trust_root_fingerprint: str
    surface_delta: SurfaceDelta
    feed_sha256: str
    checked_at: str
    checked_at_chain_anchor: str
    pinned_version: str | None = None
    offline_profile: bool = False

    @property
    def update_available(self) -> bool:
        """True when a verified newer candidate exists."""
        return self.candidate_version is not None

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical, JSON-serialisable advisory preimage."""
        return {
            "schema_version": UPDATE_ADVISORY_SCHEMA_VERSION,
            "kind": ADVISORY_KIND,
            "package": PACKAGE_NAME,
            "installed_version": self.installed_version,
            "candidate_version": self.candidate_version,
            "candidate_wheel_name": self.candidate_wheel_name,
            "candidate_wheel_sha256": self.candidate_wheel_sha256,
            "provenance_verified": self.provenance_verified,
            "provenance_source": self.provenance_source,
            "trust_root_fingerprint": self.trust_root_fingerprint,
            "surface_delta": self.surface_delta.to_dict(),
            "feed_sha256": self.feed_sha256,
            "checked_at": self.checked_at,
            "checked_at_chain_anchor": self.checked_at_chain_anchor,
            "pinned_version": self.pinned_version,
            "offline_profile": self.offline_profile,
        }


def advisory_sha256(preimage: Mapping[str, Any]) -> str:
    """Content hash (identity) of an advisory preimage's canonical bytes."""
    return _sha256_hex(_canonical(dict(preimage)))


def build_update_advisory(
    feed: ReleaseFeed,
    *,
    installed_version: str,
    chain_anchor: str,
    trust_root_pem: str,
    provenance_source: str = "signed-release-feed",
    pinned_version: str | None = None,
    offline_profile: bool = False,
    checked_at: str | None = None,
) -> UpdateAdvisory:
    """Build the advisory for *installed_version* against a **verified** feed.

    The caller must have run :func:`verify_release_feed_document` first: this
    function stamps ``provenance_verified=True`` for the candidate it selects
    precisely because the only path to a :class:`ReleaseFeed` a caller should
    use is the verified one. An unverified feed has no business reaching here,
    which is why there is no "verified" flag to pass in and get wrong.
    """
    candidate = select_candidate(feed, installed_version=installed_version, pinned_version=pinned_version)
    delta = classify_surface_delta(
        feed.releases,
        installed_version=installed_version,
        candidate_version=candidate.version if candidate else installed_version,
    )
    return UpdateAdvisory(
        installed_version=installed_version,
        candidate_version=candidate.version if candidate else None,
        candidate_wheel_name=candidate.wheel_name if candidate else None,
        candidate_wheel_sha256=candidate.wheel_sha256 if candidate else None,
        provenance_verified=candidate is not None,
        provenance_source=provenance_source,
        trust_root_fingerprint=trust_root_fingerprint(trust_root_pem),
        surface_delta=delta,
        feed_sha256=feed.body_sha256(),
        checked_at=checked_at or _utc_now_iso(),
        checked_at_chain_anchor=chain_anchor,
        pinned_version=pinned_version,
        offline_profile=offline_profile,
    )


def seal_advisory(
    advisory: UpdateAdvisory,
    *,
    private_key_pem: bytes,
    public_key_pem: str,
    kid: str = "install-identity",
) -> dict[str, Any]:
    """Seal *advisory* into a signed, self-describing document."""
    preimage = advisory.to_dict()
    canonical = _canonical(preimage)
    return {
        "advisory": preimage,
        "advisory_sha256": _sha256_hex(canonical),
        "signature": sign_detached_jws_over_canonical(
            canonical,
            private_key_pem,
            typ=ADVISORY_JWS_TYP,
            kid=kid,
        ),
        "public_key": public_key_pem,
    }


@dataclass(frozen=True, slots=True)
class AdvisoryVerification:
    """Result of re-checking a sealed advisory offline.

    Attributes:
        ok: True only when every check below passed.
        reason: Operator-facing explanation, always set.
        content_hash_ok: The recorded ``advisory_sha256`` matched the
            recomputed canonical hash.
        signature_ok: The detached JWS verified against the embedded key.
        chain_anchor: The audit-chain head the advisory was anchored to.
            Empty when the advisory carried none -- which is a refusal, not a
            warning: a recommendation with no chain position is a version diff.
    """

    ok: bool
    reason: str
    content_hash_ok: bool = False
    signature_ok: bool = False
    chain_anchor: str = ""
    advisory: dict[str, Any] | None = None

    def __bool__(self) -> bool:
        return self.ok


def verify_advisory_document(doc: Any) -> AdvisoryVerification:
    """Recompute a sealed advisory offline.

    Requires, in order: a well-formed envelope, a non-empty chain anchor, a
    matching content hash, and a valid detached JWS. Stripping the signature
    or blanking the chain anchor makes this return ``ok=False`` -- the two
    properties that separate a receipt from a printed version comparison.

    Never raises on hostile input.
    """
    if not isinstance(doc, dict):
        return AdvisoryVerification(ok=False, reason="advisory document must be an object")
    envelope = cast("dict[str, Any]", doc)
    raw_preimage: Any = envelope.get("advisory")
    if not isinstance(raw_preimage, dict):
        return AdvisoryVerification(ok=False, reason="advisory document carries no 'advisory' body")
    preimage = cast("dict[str, Any]", raw_preimage)
    if preimage.get("kind") != ADVISORY_KIND:
        return AdvisoryVerification(ok=False, reason=f"advisory kind is not {ADVISORY_KIND!r}")
    if preimage.get("schema_version") != UPDATE_ADVISORY_SCHEMA_VERSION:
        return AdvisoryVerification(
            ok=False,
            reason=f"unsupported advisory schema_version {preimage.get('schema_version')!r}",
        )

    anchor: Any = preimage.get("checked_at_chain_anchor")
    if not isinstance(anchor, str) or not anchor:
        return AdvisoryVerification(
            ok=False,
            reason="advisory carries no chain anchor; an unanchored recommendation is not a receipt",
        )

    canonical = _canonical(preimage)
    recomputed = _sha256_hex(canonical)
    recorded: Any = envelope.get("advisory_sha256")
    content_ok = isinstance(recorded, str) and recorded == recomputed
    if not content_ok:
        return AdvisoryVerification(
            ok=False,
            reason="advisory content hash does not match its body (tampered or truncated)",
            chain_anchor=anchor,
        )

    signature: Any = envelope.get("signature")
    public_key: Any = envelope.get("public_key")
    if not isinstance(signature, str) or not signature:
        return AdvisoryVerification(
            ok=False,
            reason="advisory carries no signature; an unsigned recommendation is not a receipt",
            content_hash_ok=True,
            chain_anchor=anchor,
        )
    if not isinstance(public_key, str) or not public_key.strip():
        return AdvisoryVerification(
            ok=False,
            reason="advisory carries no public key",
            content_hash_ok=True,
            chain_anchor=anchor,
        )
    signature_ok = verify_detached_jws_over_canonical(
        canonical,
        signature,
        _normalised_pem(public_key).encode("ascii") + b"\n",
        expected_typ=ADVISORY_JWS_TYP,
    )
    if not signature_ok:
        return AdvisoryVerification(
            ok=False,
            reason="advisory signature does not verify",
            content_hash_ok=True,
            chain_anchor=anchor,
        )
    return AdvisoryVerification(
        ok=True,
        reason="advisory content hash, signature, and chain anchor all check out",
        content_hash_ok=True,
        signature_ok=True,
        chain_anchor=anchor,
        advisory=dict(preimage),
    )


# ---------------------------------------------------------------------------
# Cache (so `doctor` and the version hint never touch the network)
# ---------------------------------------------------------------------------


def cache_path(home: Path | None = None) -> Path:
    """Return the cached-advisory path (``~/.bernstein/update-advisory.json``)."""
    from pathlib import Path as _Path

    base = home if home is not None else _Path.home()
    return base.joinpath(*_CACHE_RELPATH)


def store_cached_advisory(document: Mapping[str, Any], *, home: Path | None = None) -> Path:
    """Persist a sealed advisory document to the cache and return its path."""
    path = cache_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(document), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def load_cached_advisory(home: Path | None = None) -> dict[str, Any] | None:
    """Read the cached advisory document, or ``None`` when absent/unreadable.

    Purely local: this is the function ``bernstein doctor`` and the
    ``--version`` hint call, and it can never trigger a network request.
    """
    path = cache_path(home)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return cast("dict[str, Any]", parsed) if isinstance(parsed, dict) else None


def cache_is_fresh(
    document: Mapping[str, Any],
    *,
    now: _dt.datetime | None = None,
    min_interval_s: int = DEFAULT_MIN_CHECK_INTERVAL_S,
) -> bool:
    """True when the cached advisory is younger than the rate-limit window."""
    preimage: Any = document.get("advisory")
    if not isinstance(preimage, dict):
        return False
    checked_at: Any = cast("dict[str, Any]", preimage).get("checked_at")
    if not isinstance(checked_at, str):
        return False
    try:
        stamped = _dt.datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if stamped.tzinfo is None:
        stamped = stamped.replace(tzinfo=_dt.UTC)
    reference = now or _dt.datetime.now(tz=_dt.UTC)
    return (reference - stamped).total_seconds() < min_interval_s


# ---------------------------------------------------------------------------
# Version pin
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VersionPin:
    """A signed operator pin the updater refuses to cross without an override."""

    version: str
    pinned_at: str
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical, JSON-serialisable pin preimage."""
        return {
            "schema_version": UPDATE_ADVISORY_SCHEMA_VERSION,
            "kind": "self.version_pin",
            "package": PACKAGE_NAME,
            "version": self.version,
            "pinned_at": self.pinned_at,
            "reason": self.reason,
        }


def pin_path(home: Path | None = None) -> Path:
    """Return the version-pin path (``~/.bernstein/version-pin.json``)."""
    from pathlib import Path as _Path

    base = home if home is not None else _Path.home()
    return base.joinpath(*_PIN_RELPATH)


def write_version_pin(
    pin: VersionPin,
    *,
    private_key_pem: bytes,
    public_key_pem: str,
    home: Path | None = None,
) -> Path:
    """Seal and persist a version pin; returns the path written."""
    preimage = pin.to_dict()
    canonical = _canonical(preimage)
    document = {
        "pin": preimage,
        "pin_sha256": _sha256_hex(canonical),
        "signature": sign_detached_jws_over_canonical(
            canonical,
            private_key_pem,
            typ=PIN_JWS_TYP,
            kid="install-identity",
        ),
        "public_key": public_key_pem,
    }
    path = pin_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def read_version_pin(home: Path | None = None) -> tuple[VersionPin | None, str]:
    """Read and re-verify the version pin.

    Returns:
        ``(pin, reason)``. ``pin`` is ``None`` when no pin is installed *or*
        when the stored pin fails its own signature check -- a pin that does
        not verify is refused loudly rather than silently ignored, because
        "the pin was tampered with" and "there is no pin" must not look the
        same to an operator.
    """
    path = pin_path(home)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None, "no version pin installed"
    try:
        doc: Any = json.loads(raw)
    except json.JSONDecodeError:
        return None, f"version pin {path} is not valid JSON"
    if not isinstance(doc, dict):
        return None, f"version pin {path} must contain a JSON object"
    envelope = cast("dict[str, Any]", doc)
    raw_pin: Any = envelope.get("pin")
    signature: Any = envelope.get("signature")
    public_key: Any = envelope.get("public_key")
    if not isinstance(raw_pin, dict) or not isinstance(signature, str) or not isinstance(public_key, str):
        return None, f"version pin {path} is malformed"
    preimage = cast("dict[str, Any]", raw_pin)
    canonical = _canonical(preimage)
    if envelope.get("pin_sha256") != _sha256_hex(canonical):
        return None, f"version pin {path} content hash does not match its body"
    if not verify_detached_jws_over_canonical(
        canonical,
        signature,
        _normalised_pem(public_key).encode("ascii") + b"\n",
        expected_typ=PIN_JWS_TYP,
    ):
        return None, f"version pin {path} signature does not verify"
    version: Any = preimage.get("version")
    if not isinstance(version, str) or not version:
        return None, f"version pin {path} names no version"
    return (
        VersionPin(
            version=version,
            pinned_at=str(preimage.get("pinned_at", "")),
            reason=str(preimage.get("reason", "")),
        ),
        f"version pin {version} verified",
    )


def pin_blocks(pin: VersionPin | None, target_version: str) -> bool:
    """True when *target_version* is above the pin and must not be crossed."""
    if pin is None:
        return False
    return compare_versions(target_version, pin.version) > 0


# ---------------------------------------------------------------------------
# Pre-install verification
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WheelVerification:
    """Outcome of checking a downloaded wheel against the advisory.

    Attributes:
        ok: True only when the recomputed sha256 equals the advisory's
            provenance-checked hash, and (when required) the Sigstore
            attestation verified.
        reason: Operator-facing explanation, always set.
        actual_sha256: What the file on disk actually hashes to.
        expected_sha256: What the signed feed said it must be.
        attestation_ok: Tri-state Sigstore result -- True verified, False hard
            failure, None skipped (no ``gh``, offline, no attestation).
    """

    ok: bool
    reason: str
    actual_sha256: str = ""
    expected_sha256: str = ""
    attestation_ok: bool | None = None

    def __bool__(self) -> bool:
        return self.ok


def verify_wheel_against_advisory(
    wheel: Path,
    *,
    advisory: Mapping[str, Any],
    require_attestation: bool = False,
    sigstore_offline: bool = False,
    sigstore_bundle_dir: Path | None = None,
) -> WheelVerification:
    """Verify a downloaded wheel **before** it is installed.

    Two independent chains have to agree:

    1. The wheel's sha256 must equal the hash carried by the signed release
       feed (already verified against the trust root when the advisory was
       built). This is the mandatory check -- a mismatch aborts.
    2. The Sigstore build-provenance attestation the release pipeline emits is
       re-checked through the existing verifier. It can only escalate a
       failure, never mask the hash check. With *require_attestation* a
       skipped attestation is also a refusal, which is the setting an operator
       under a supply-chain policy wants.
    """
    expected = advisory.get("candidate_wheel_sha256")
    if not isinstance(expected, str) or not expected:
        return WheelVerification(ok=False, reason="advisory names no candidate wheel hash; refusing to install")
    if not wheel.is_file():
        return WheelVerification(ok=False, reason=f"wheel not found: {wheel}", expected_sha256=expected)
    actual = sha256_file(wheel)
    if actual != expected.lower():
        return WheelVerification(
            ok=False,
            reason=(
                f"wheel hash mismatch: {wheel.name} hashes to {actual}, "
                f"the provenance-verified advisory expects {expected}"
            ),
            actual_sha256=actual,
            expected_sha256=expected,
        )

    from bernstein.core.distribution.sigstore_attestation_verify import (
        SigstoreAttestationVerifier,
        verify_artefacts_with_sigstore,
    )

    report = verify_artefacts_with_sigstore(
        [wheel],
        verifier=SigstoreAttestationVerifier(offline=sigstore_offline, bundle_dir=sigstore_bundle_dir),
        require_attestation=require_attestation,
    )
    if report.ok is False:
        return WheelVerification(
            ok=False,
            reason="; ".join(report.failures) or "Sigstore attestation verification failed",
            actual_sha256=actual,
            expected_sha256=expected,
            attestation_ok=False,
        )
    if report.ok is None:
        return WheelVerification(
            ok=True,
            reason=(
                "wheel hash matches the provenance-verified advisory; "
                f"Sigstore attestation skipped ({'; '.join(report.skips) or 'unavailable'})"
            ),
            actual_sha256=actual,
            expected_sha256=expected,
            attestation_ok=None,
        )
    return WheelVerification(
        ok=True,
        reason="wheel hash matches the provenance-verified advisory and its Sigstore attestation verified",
        actual_sha256=actual,
        expected_sha256=expected,
        attestation_ok=True,
    )


# ---------------------------------------------------------------------------
# Install / rollback receipt preimages
# ---------------------------------------------------------------------------


def build_install_receipt(
    *,
    from_version: str,
    to_version: str,
    wheel_sha256: str,
    provenance_key_fingerprint: str,
    advisory_sha256_value: str,
    direction: str,
    chain_anchor: str,
    attestation_ok: bool | None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Bind one install (or rollback) into a canonical receipt preimage.

    Deterministic: a pure function of its arguments, so the receipt the CLI
    prints, the receipt written to disk, and the payload appended to the audit
    chain are the same bytes under the same hash.
    """
    return {
        "schema_version": UPDATE_ADVISORY_SCHEMA_VERSION,
        "kind": "self.update_receipt",
        "package": PACKAGE_NAME,
        "direction": direction,
        "from_version": from_version,
        "to_version": to_version,
        "wheel_sha256": wheel_sha256,
        "provenance_key_fingerprint": provenance_key_fingerprint,
        "advisory_sha256": advisory_sha256_value,
        "attestation_verified": attestation_ok,
        "checked_at_chain_anchor": chain_anchor,
        "generated_at": generated_at or _utc_now_iso(),
    }


def receipt_sha256(receipt: Mapping[str, Any]) -> str:
    """Content hash (identity) of an install/rollback receipt preimage."""
    return _sha256_hex(_canonical(dict(receipt)))


def receipts_dir(home: Path | None = None) -> Path:
    """Return the local install-receipt store directory."""
    from pathlib import Path as _Path

    base = home if home is not None else _Path.home()
    return base / ".bernstein" / "update-receipts"


def store_receipt(receipt: Mapping[str, Any], *, home: Path | None = None) -> Path:
    """Persist an install/rollback receipt, content-addressed by its hash."""
    digest = receipt_sha256(receipt)
    directory = receipts_dir(home)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{digest}.json"
    path.write_text(
        json.dumps({"receipt": dict(receipt), "receipt_sha256": digest}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def read_receipts(home: Path | None = None) -> list[dict[str, Any]]:
    """Return every stored receipt whose content hash still checks out.

    A receipt whose recorded hash does not match its body is dropped rather
    than returned with a warning: the rollback target is chosen from this
    list, and an unverifiable receipt must not be able to steer an install.
    """
    directory = receipts_dir(home)
    if not directory.is_dir():
        return []
    receipts: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            doc: Any = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(doc, dict):
            continue
        envelope = cast("dict[str, Any]", doc)
        raw_body: Any = envelope.get("receipt")
        if not isinstance(raw_body, dict):
            continue
        body = cast("dict[str, Any]", raw_body)
        if envelope.get("receipt_sha256") != receipt_sha256(body):
            continue
        receipts.append(body)
    receipts.sort(key=lambda r: str(r.get("generated_at", "")))
    return receipts


def previous_receipted_version(home: Path | None = None) -> str | None:
    """Return the version to roll back to, from the receipted install history.

    The predecessor is derived from the receipt chain rather than from a
    plaintext breadcrumb file, so "roll back" means "return to the version the
    chain says you were on" instead of "whatever the last write left behind".
    """
    for receipt in reversed(read_receipts(home)):
        if receipt.get("direction") == "install":
            candidate: Any = receipt.get("from_version")
            if isinstance(candidate, str) and candidate and candidate != "unknown":
                return candidate
    return None


def feed_cache_path(home: Path | None = None) -> Path:
    """Return the cached verified-feed path (``~/.bernstein/release-feed.json``)."""
    from pathlib import Path as _Path

    base = home if home is not None else _Path.home()
    return base / ".bernstein" / "release-feed.json"


def store_cached_feed(document: Mapping[str, Any], *, home: Path | None = None) -> Path:
    """Persist the release-feed document that last verified, and return its path.

    Keeping the *signed* feed rather than a digest of it is what lets a later
    rollback resolve its target's wheel hash offline and still re-verify the
    whole document against the trust root first.
    """
    path = feed_cache_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(document), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def load_cached_feed(home: Path | None = None) -> dict[str, Any] | None:
    """Read the cached release-feed document, or ``None`` when absent."""
    path = feed_cache_path(home)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return cast("dict[str, Any]", parsed) if isinstance(parsed, dict) else None


def install_identity_pems(root: Path) -> tuple[bytes, str]:
    """Return ``(private_pkcs8_pem, public_spki_pem)`` for the install identity.

    The advisory and the version pin are signed with the same Ed25519 install
    key that already anchors credential manifests and OTel span projections,
    so every artefact this install signs shares one attestation root.
    """
    from cryptography.hazmat.primitives import serialization

    from bernstein.core.security.install_key import load_or_create_install_key, signing_key_path

    private_key = load_or_create_install_key(signing_key_path(root))
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem.decode("ascii")
