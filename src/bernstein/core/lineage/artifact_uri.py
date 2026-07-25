"""Canonical artifact-key grammar for the lineage layer (issue #2559).

Provenance keys used to be repo-relative POSIX paths and nothing else, so the
moment an output left the worktree it lost its provenance identity: a published
package, a release PR or a deployed docs page had no key the chain could answer
questions about. This module widens the key space *without* widening what the
boundaries accept.

Grammar
-------

An **artifact key** is one of:

``<repo-relative POSIX path>``
    The implicit default scheme. ``src/bernstein/core/foo.py``. This is what
    every historical lineage entry carries, and it is left byte-for-byte
    untouched: the repo branch of this module applies exactly the rules the
    boundaries applied before (empty / absolute / ``..`` traversal), so old
    records keep their exact entry hashes, HMAC tags and signatures.

``<scheme>://<authority>/<segment>[/<segment>...]``
    An external artifact. The scheme set is **closed**:

    =========  ==================================  ===================================
    scheme     canonical shape                     example
    =========  ==================================  ===================================
    ``pr``     ``pr://<host>/<project…>/<number>``  ``pr://github.com/acme/widget/2559``
    ``pkg``    ``pkg://<ecosystem>/<name…>/<ver>``  ``pkg://pypi/bernstein/3.9.0``
    ``deploy`` ``deploy://<env>/<target…>``         ``deploy://prod/docs-site``
    ``doc``    ``doc://<host>/<path…>``             ``doc://example.test/lineage``
    =========  ==================================  ===================================

    ``repo://<path>`` is accepted by :func:`canonical_artifact_key` as an
    *input alias* whose canonical form is the bare path. It is therefore never
    a valid on-wire key: writing ``repo://x`` and ``x`` as two keys for one
    artifact would fork its tip set.

Why the boundaries only accept canonical keys
---------------------------------------------

A lineage key is an identity. Two spellings of one artifact (``pr://GitHub.com``
vs ``pr://github.com``, a stray trailing slash) would split that artifact's
history across two chains and silently break fork detection. The boundary
therefore *validates* rather than rewrites: a non-canonical URI is rejected with
its canonical form named in the error, so the caller fixes the key rather than
having it silently changed underneath a hash that was already computed.

Determinism
-----------

Canonicalisation is a pure function of the input string. No filesystem access,
no environment lookup, no host paths, no ordering dependence -- two operators on
different machines derive the same canonical key and the same content address
from the same declared output.

Tightening relative to the pre-#2559 boundaries
-----------------------------------------------

One behaviour changes on purpose. A string containing ``://`` used to sail
through the repo-path checks (``ftp://evil/x`` has no leading ``/``, no drive
prefix and no ``..`` segment, so it was stored verbatim as if it were a repo
path). Such a string is now parsed as a URI and rejected unless its scheme is in
the closed set. Repo-relative paths, which by construction contain no ``://``,
are unaffected.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from functools import lru_cache

__all__ = [
    "ARTIFACT_URI_SCHEMES",
    "EXTERNAL_ARTEFACT_KIND",
    "KNOWN_ARTIFACT_SCHEMES",
    "REASON_ABSOLUTE",
    "REASON_EMPTY",
    "REASON_MALFORMED_URI",
    "REASON_NON_CANONICAL",
    "REASON_TRAVERSAL",
    "REASON_UNKNOWN_SCHEME",
    "REPO_SCHEME",
    "ArtifactKey",
    "ArtifactURIError",
    "artifact_key_rejection_reason",
    "canonical_artifact_key",
    "canonical_artifact_pattern",
    "external_reference_bytes",
    "external_reference_content_hash",
    "is_canonical_artifact_key",
    "is_glob_pattern",
    "looks_like_artifact_uri",
    "match_artifact_pattern",
    "parse_artifact_key",
]

#: Closed set of external artifact URI schemes. Membership may only widen in a
#: future release; an unknown scheme is rejected at the write boundaries rather
#: than passed through, because a lineage key that means nothing in particular
#: is worse than no key at all.
ARTIFACT_URI_SCHEMES: frozenset[str] = frozenset({"pr", "pkg", "deploy", "doc"})

#: The implicit scheme carried by every historical entry. Never written on the
#: wire: the canonical form of a repo artifact is the bare repo-relative path.
REPO_SCHEME = "repo"

#: Every scheme :func:`parse_artifact_key` recognises, including the implicit
#: repo scheme and its ``repo://`` input alias.
KNOWN_ARTIFACT_SCHEMES: frozenset[str] = ARTIFACT_URI_SCHEMES | {REPO_SCHEME}

#: ``artefact_kind`` recorded on a v1 lineage entry that anchors an external
#: artifact by reference rather than by its own bytes. Kept in step with
#: :data:`bernstein.core.lineage.entry.ARTEFACT_KINDS`.
EXTERNAL_ARTEFACT_KIND = "external"

# --- rejection reason codes -------------------------------------------------
#
# The two write boundaries (``LineageSpine._reject_unsafe_artifact_path`` and
# the v1 signed-write ``_is_unsafe_path``) word their errors differently and
# spell the field differently (``artifact_path`` vs ``artefact_path``). They
# share this decision function and map the code onto their own message, so the
# pre-#2559 error strings survive verbatim and no caller's error handling
# changes.

REASON_EMPTY = "empty"
REASON_ABSOLUTE = "absolute"
REASON_TRAVERSAL = "traversal"
REASON_UNKNOWN_SCHEME = "unknown-scheme"
REASON_MALFORMED_URI = "malformed-uri"
REASON_NON_CANONICAL = "non-canonical-uri"

_SCHEME_MARK = "://"
_SCHEME_RE = re.compile(r"\A([A-Za-z][A-Za-z0-9+.\-]*)://")

# A DNS-ish host with an optional port. Lowercased on canonicalisation because
# DNS is case-insensitive, so two spellings must not key two artifacts.
_HOST_LABEL = r"[a-z0-9](?:[a-z0-9\-]*[a-z0-9])?"
_HOST_RE = re.compile(rf"\A{_HOST_LABEL}(?:\.{_HOST_LABEL})*(?::[1-9][0-9]{{0,4}})?\Z")

# Ecosystem / environment authority tokens (``pypi``, ``npm``, ``oci``,
# ``prod``, ``staging``). Lowercased for the same reason as the host.
_TOKEN_RE = re.compile(r"\A[a-z0-9][a-z0-9._+\-]*\Z")

# Path segments keep their case: package and repository names are
# case-sensitive on npm and on most forges, and folding them would conflate
# genuinely distinct artifacts. Percent-encoding is rejected outright -- two
# encodings of one byte would be two keys for one artifact.
_SEGMENT_RE = re.compile(r"\A[A-Za-z0-9._~@+\-]+\Z")
_SEGMENT_GLOB_RE = re.compile(r"\A[A-Za-z0-9._~@+\-*?]+\Z")

_PR_NUMBER_RE = re.compile(r"\A[1-9][0-9]*\Z")

_MAX_KEY_LEN = 2048
_MAX_SEGMENT_LEN = 255

#: Minimum number of path segments each scheme requires. ``pr`` needs a project
#: path plus a number; ``pkg`` needs a name plus a version.
_MIN_SEGMENTS = {"pr": 3, "pkg": 2, "deploy": 1, "doc": 1}

_GLOB_CHARS = frozenset("*?")

_DIGEST_ALGS = frozenset({"sha1", "sha256", "sha512"})
_DIGEST_HEX_LEN = {"sha1": 40, "sha256": 64, "sha512": 128}

#: Version stamped into the external reference document. Bump only on a change
#: to the anchored bytes; the content address is derived from these bytes.
_REFERENCE_VERSION = 1


class ArtifactURIError(ValueError):
    """Raised when an artifact key cannot be parsed or is not canonical."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ArtifactKey:
    """A parsed artifact key.

    Attributes:
        scheme: One of :data:`KNOWN_ARTIFACT_SCHEMES`. ``repo`` for a
            repo-relative path.
        authority: Host or token for an external scheme; ``""`` for ``repo``.
        segments: Path segments. For ``repo`` this is the whole path split on
            ``/`` -- but see :attr:`canonical`, which reproduces the original
            string verbatim rather than rejoining, so a repo path is never
            rewritten.
        canonical: The single legal on-wire spelling of this key.
    """

    scheme: str
    authority: str
    segments: tuple[str, ...]
    canonical: str

    @property
    def is_repo(self) -> bool:
        """Whether this key is a repo-relative path (the implicit scheme)."""
        return self.scheme == REPO_SCHEME

    def to_dict(self) -> dict[str, object]:
        return {
            "scheme": self.scheme,
            "authority": self.authority,
            "segments": list(self.segments),
            "canonical": self.canonical,
        }


def looks_like_artifact_uri(raw: str) -> bool:
    """Whether ``raw`` is routed through the URI branch rather than repo rules.

    The test is deliberately the crude presence of ``://`` rather than a
    successful scheme match: a string that carries the URI marker but no valid
    scheme is *malformed*, not a repo path that happens to contain punctuation.
    Falling back to the repo branch there is exactly the hole that let an
    unknown scheme be stored as if it were a filename.
    """
    return _SCHEME_MARK in raw


def _repo_rejection_reason(raw: str) -> str | None:
    """Apply the pre-#2559 repo-path rules, unchanged.

    Kept byte-identical to the two boundary guards it replaces so that every
    repo-relative path accepted (or rejected) before is accepted (or rejected)
    now, with the same verdict:

      * empty paths are rejected;
      * POSIX absolute (``/foo``) and Windows drive-prefixed (``C:\\foo``)
        paths are rejected;
      * any ``..`` segment is rejected, treating ``\\`` as a separator too
        (defence in depth -- lineage canonical is POSIX).
    """
    if not raw:
        return REASON_EMPTY
    if raw.startswith("/") or (len(raw) > 2 and raw[1:3] == ":\\"):
        return REASON_ABSOLUTE
    if any(seg == ".." for seg in raw.replace("\\", "/").split("/")):
        return REASON_TRAVERSAL
    return None


def _has_forbidden_char(raw: str) -> bool:
    """Whether ``raw`` carries a character no artifact key may contain.

    Whitespace, C0/C1 control characters and NUL cannot appear in a key: they
    are invisible in an audit trail, they let two visually identical keys hash
    differently, and NUL truncates in every C-string boundary the key crosses.
    """
    return any(ch.isspace() or ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F for ch in raw)


def _parse_uri(raw: str, *, allow_glob: bool) -> ArtifactKey:
    """Parse the URI branch. Raises :class:`ArtifactURIError` on any problem.

    Every hardening rule that does not exist for repo paths lives here rather
    than in :func:`_parse`. Repo paths carry whatever the filesystem allowed --
    spaces, ``*``, unusual punctuation -- and tightening that would change the
    accept set for keys already in existing chains.
    """
    if len(raw) > _MAX_KEY_LEN:
        raise ArtifactURIError(f"artifact URI longer than {_MAX_KEY_LEN} characters", reason=REASON_MALFORMED_URI)
    if _has_forbidden_char(raw):
        raise ArtifactURIError(
            f"whitespace or control character in artifact URI: {raw!r}",
            reason=REASON_MALFORMED_URI,
        )
    match = _SCHEME_RE.match(raw)
    if match is None:
        raise ArtifactURIError(
            f"artifact key carries {_SCHEME_MARK!r} but no valid scheme: {raw!r}",
            reason=REASON_MALFORMED_URI,
        )
    scheme = match.group(1).lower()
    if scheme not in KNOWN_ARTIFACT_SCHEMES:
        raise ArtifactURIError(
            f"unknown artifact URI scheme {scheme!r}; known schemes are {sorted(KNOWN_ARTIFACT_SCHEMES)}",
            reason=REASON_UNKNOWN_SCHEME,
        )
    rest = raw[match.end() :]

    if scheme == REPO_SCHEME:
        # ``repo://`` is an input alias: its canonical form is the bare path,
        # so it always fails the canonicality check at a write boundary while
        # still being usable in an operator-authored declaration.
        if _SCHEME_MARK in rest:
            raise ArtifactURIError(
                f"nested scheme marker in repo artifact key: {raw!r}",
                reason=REASON_MALFORMED_URI,
            )
        repo_reason = _repo_rejection_reason(rest)
        if repo_reason is not None:
            raise ArtifactURIError(f"unsafe repo artifact key: {raw!r}", reason=repo_reason)
        return ArtifactKey(
            scheme=REPO_SCHEME,
            authority="",
            segments=tuple(rest.split("/")),
            canonical=rest,
        )

    if _SCHEME_MARK in rest:
        raise ArtifactURIError(f"nested scheme marker in artifact URI: {raw!r}", reason=REASON_MALFORMED_URI)
    if "%" in raw:
        raise ArtifactURIError(
            f"percent-encoding is not allowed in an artifact URI: {raw!r}",
            reason=REASON_MALFORMED_URI,
        )
    if "\\" in raw:
        raise ArtifactURIError(f"backslash is not allowed in an artifact URI: {raw!r}", reason=REASON_MALFORMED_URI)

    parts = rest.split("/")
    authority = parts[0].lower()
    segments = parts[1:]
    # Exactly one trailing slash is tolerated and dropped; anything else that
    # produces an empty component (``//``) is a malformed key, not a key to
    # silently repair.
    if segments and segments[-1] == "":
        segments = segments[:-1]
    if any(seg == "" for seg in segments):
        raise ArtifactURIError(f"empty path segment in artifact URI: {raw!r}", reason=REASON_MALFORMED_URI)

    if not authority:
        raise ArtifactURIError(f"missing authority in artifact URI: {raw!r}", reason=REASON_MALFORMED_URI)
    authority_re = _HOST_RE if scheme in {"pr", "doc"} else _TOKEN_RE
    if authority_re.match(authority) is None:
        raise ArtifactURIError(
            f"invalid authority {authority!r} for {scheme}:// artifact URI",
            reason=REASON_MALFORMED_URI,
        )

    segment_re = _SEGMENT_GLOB_RE if allow_glob else _SEGMENT_RE
    for seg in segments:
        if seg in {".", ".."}:
            raise ArtifactURIError(f"path traversal in artifact URI: {raw!r}", reason=REASON_TRAVERSAL)
        if len(seg) > _MAX_SEGMENT_LEN:
            raise ArtifactURIError(f"artifact URI segment too long: {raw!r}", reason=REASON_MALFORMED_URI)
        if segment_re.match(seg) is None:
            raise ArtifactURIError(f"invalid character in artifact URI segment {seg!r}", reason=REASON_MALFORMED_URI)

    globbed = allow_glob and any(ch in _GLOB_CHARS for seg in segments for ch in seg)
    if not globbed:
        # Arity and the ``pr`` number shape are structural facts about a
        # concrete artifact. A pattern names a *set* of artifacts, so it is
        # exempt: ``pkg://pypi/bernstein/*`` is a legitimate declaration.
        minimum = _MIN_SEGMENTS[scheme]
        if len(segments) < minimum:
            raise ArtifactURIError(
                f"{scheme}:// artifact URI needs at least {minimum} path segment(s): {raw!r}",
                reason=REASON_MALFORMED_URI,
            )
        if scheme == "pr" and _PR_NUMBER_RE.match(segments[-1]) is None:
            raise ArtifactURIError(
                f"pr:// artifact URI must end in a decimal request number: {raw!r}",
                reason=REASON_MALFORMED_URI,
            )

    canonical = f"{scheme}://{authority}/" + "/".join(segments)
    return ArtifactKey(scheme=scheme, authority=authority, segments=tuple(segments), canonical=canonical)


def _parse(raw: str, *, allow_glob: bool) -> ArtifactKey:
    """Route ``raw`` to the URI branch or the untouched repo branch.

    The repo branch applies the pre-#2559 rules and nothing else. Any extra
    strictness -- length caps, control-character rejection, charset limits --
    belongs to the URI branch alone, because a repo path that an existing chain
    already records must keep resolving to the same verdict.
    """
    if not isinstance(raw, str):  # pragma: no cover - defensive, callers are typed
        raise ArtifactURIError(f"artifact key must be a string, got {type(raw).__name__}", reason=REASON_MALFORMED_URI)
    if looks_like_artifact_uri(raw):
        return _parse_uri(raw, allow_glob=allow_glob)

    repo_reason = _repo_rejection_reason(raw)
    if repo_reason is not None:
        raise ArtifactURIError(f"unsafe repo artifact key: {raw!r}", reason=repo_reason)
    # A repo path is reproduced verbatim: canonicalisation must never rewrite
    # the key historical entries were hashed under.
    return ArtifactKey(scheme=REPO_SCHEME, authority="", segments=tuple(raw.split("/")), canonical=raw)


def parse_artifact_key(raw: str) -> ArtifactKey:
    """Parse a concrete artifact key. Raises :class:`ArtifactURIError`."""
    return _parse(raw, allow_glob=False)


def canonical_artifact_key(raw: str) -> str:
    """Return the single legal on-wire spelling of ``raw``.

    Lowercases the scheme and authority, drops a single trailing slash, and
    reduces ``repo://<path>`` to the bare ``<path>``. Repo-relative paths are
    returned unchanged. Raises :class:`ArtifactURIError` for anything that is
    not a valid key.
    """
    return parse_artifact_key(raw).canonical


def canonical_artifact_pattern(raw: str) -> str:
    """Return the canonical spelling of a declared-output pattern.

    Same grammar as :func:`canonical_artifact_key`, except that ``*``, ``**``
    and ``?`` are legal inside path segments and the per-scheme arity rules are
    relaxed for a pattern that actually uses them.
    """
    return _parse(raw, allow_glob=True).canonical


def is_glob_pattern(raw: str) -> bool:
    """Whether ``raw`` uses glob metacharacters, i.e. names a set not an artifact.

    A repo-relative key accepts ``*`` and ``?`` as ordinary filename characters,
    so parsing alone cannot tell a pattern from a key: the distinction has to be
    made against the raw declaration. Callers that need one concrete artifact --
    an attempt record has to be keyed under a single URI -- use this to skip the
    declarations that name a set.
    """
    return any(ch in _GLOB_CHARS for ch in raw)


def is_canonical_artifact_key(raw: str) -> bool:
    """Whether ``raw`` is already the canonical spelling of a valid key."""
    try:
        return canonical_artifact_key(raw) == raw
    except ArtifactURIError:
        return False


def artifact_key_rejection_reason(raw: str) -> str | None:
    """Return why a write boundary must reject ``raw``, or ``None`` to accept.

    This is the single decision function behind both lineage write boundaries.
    It accepts only *canonical* keys: a valid but non-canonical URI is rejected
    rather than rewritten, because rewriting would change the key an entry hash
    was computed under, and accepting both spellings would fork the artifact's
    tip set across two chains.

    Repo-relative paths take the pre-#2559 branch unchanged, so their accept /
    reject verdict is byte-identical to the guards this replaces.

    Returns:
        One of the ``REASON_*`` codes, or ``None`` when ``raw`` is acceptable.
    """
    try:
        key = _parse(raw, allow_glob=False)
    except ArtifactURIError as exc:
        return exc.reason
    if key.canonical != raw:
        return REASON_NON_CANONICAL
    return None


# ---------------------------------------------------------------------------
# Declared-output pattern matching
# ---------------------------------------------------------------------------


@lru_cache(maxsize=512)
def _pattern_regex(pattern: str) -> re.Pattern[str]:
    """Translate a canonical pattern into an anchored regex.

    ``**`` crosses ``/``; ``*`` and ``?`` do not. ``/**/`` additionally matches
    zero intervening segments, so ``docs/**/*.md`` covers both ``docs/a.md``
    and ``docs/nested/a.md``. Pure string translation -- the matcher never
    touches the filesystem, so the same pattern and key give the same answer on
    any host.
    """
    out: list[str] = []
    i = 0
    n = len(pattern)
    while i < n:
        if pattern.startswith("/**/", i):
            out.append(r"(?:/.*)?/")
            i += 4
        elif pattern.startswith("**", i):
            out.append(r".*")
            i += 2
        elif pattern[i] == "*":
            out.append(r"[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append(r"[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def match_artifact_pattern(pattern: str, key: str) -> bool:
    """Whether the concrete artifact ``key`` is covered by ``pattern``.

    Both sides are canonicalised first, so ``PKG://PyPI/x/1`` declared against
    a produced ``pkg://pypi/x/1`` matches. Raises :class:`ArtifactURIError` if
    either side is not a valid key or pattern.
    """
    canonical_pattern = canonical_artifact_pattern(pattern)
    canonical_key = canonical_artifact_key(key)
    if not any(ch in _GLOB_CHARS for ch in canonical_pattern):
        return canonical_pattern == canonical_key
    return _pattern_regex(canonical_pattern).match(canonical_key) is not None


# ---------------------------------------------------------------------------
# External referenced content (bytes-at-decision anchoring)
# ---------------------------------------------------------------------------


def _normalise_digest(digest: str) -> str:
    """Validate and lowercase an ``<alg>:<hex>`` digest.

    The algorithm must be named explicitly. A bare hex string is ambiguous
    (40 hex digits is a git SHA-1 today and something else tomorrow), and an
    ambiguous anchor is not an anchor.
    """
    if not isinstance(digest, str) or ":" not in digest:
        raise ArtifactURIError(
            f"digest must be '<alg>:<hex>', got {digest!r}",
            reason=REASON_MALFORMED_URI,
        )
    alg, _, hexpart = digest.partition(":")
    alg = alg.lower()
    hexpart = hexpart.lower()
    if alg not in _DIGEST_ALGS:
        raise ArtifactURIError(
            f"unsupported digest algorithm {alg!r}; supported: {sorted(_DIGEST_ALGS)}",
            reason=REASON_MALFORMED_URI,
        )
    if len(hexpart) != _DIGEST_HEX_LEN[alg] or any(c not in "0123456789abcdef" for c in hexpart):
        raise ArtifactURIError(
            f"digest for {alg} must be {_DIGEST_HEX_LEN[alg]} hex characters",
            reason=REASON_MALFORMED_URI,
        )
    return f"{alg}:{hexpart}"


def external_reference_bytes(uri: str, *, digest: str) -> bytes:
    """Return the canonical reference document anchoring an external artifact.

    An external artifact's bytes do not live in the worktree, so the lineage
    entry cannot hash them directly. It anchors them *by reference* the way
    :mod:`bernstein.core.lineage.c2pa` anchors referenced content: the entry's
    content hash covers a small canonical document naming the artifact and the
    digest it carried at decision time -- the PR head commit, the package
    archive digest, the deployed image digest, the rendered page hash.

    The document contains no host path and no timestamp, so two operators
    anchoring the same artifact at the same digest derive byte-identical bytes
    and therefore the same content address.

    Args:
        uri: An external artifact URI (``pr``/``pkg``/``deploy``/``doc``).
        digest: The referenced content digest as ``<alg>:<hex>``.

    Returns:
        RFC 8785-style canonical JSON bytes (sorted keys, minimal separators).

    Raises:
        ArtifactURIError: When ``uri`` is a repo key or either input is
            malformed. A repo artifact is anchored by its own bytes, so
            referencing it here would create a second, weaker anchor for
            content the chain can hash directly.
    """
    key = parse_artifact_key(uri)
    if key.is_repo:
        raise ArtifactURIError(
            f"repo artifacts are anchored by their own bytes, not by reference: {uri!r}",
            reason=REASON_MALFORMED_URI,
        )
    document = {
        "artifact_uri": key.canonical,
        "digest": _normalise_digest(digest),
        "v": _REFERENCE_VERSION,
    }
    return json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def external_reference_content_hash(uri: str, *, digest: str) -> str:
    """Return the ``sha256:``-prefixed content address of an external reference.

    This is the value a lineage entry records as its ``content_hash`` for an
    ``external`` artefact kind. Deterministic across hosts.
    """
    return "sha256:" + hashlib.sha256(external_reference_bytes(uri, digest=digest)).hexdigest()
