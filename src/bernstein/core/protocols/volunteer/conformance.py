"""Conformance harness: prove the same document survives GitHub ↔ hub projections.

This module provides a generic framework for testing that volunteer protocol
documents maintain their canonical identity when serialised through two different
transports:

* **Hub projection** — canonical dict serialised as plain JSON (UTF-8).
  This is the simplest possible projection and the one the verifier uses when
  reading from storage or a webhook payload.

* **GitHub projection** — the document is embedded inside a human-readable
  comment string that a volunteer posts to a GitHub issue.  The format is a
  fenced JSON block wrapped in a ``bernstein-claim`` marker so the comment can
  be reliably stripped on read-back.

The property the harness proves::

    canonical_hash(original_dict) ==
        canonical_hash(from_github_projection(to_github_projection(original_dict))) ==
        canonical_hash(from_hub_projection(to_hub_projection(original_dict)))

Both projections are **lossless** — the round-trip recovers the exact input dict.
The GitHub projection is additionally **parseable by a human** who reads the issue
comment, which is why we use a fenced JSON block rather than raw base64.

Reuse
-----

``ConformanceHarness`` is generic.  Future document types (project/worker card,
submission, verification verdict) plug in by registering a pair of
``to_canonical_dict`` / ``from_canonical_dict`` callables with the same harness
instance — no subclassing, no copy-paste.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeVar

from bernstein.core.protocols.volunteer.documents import (
    canonical_bytes,
    canonical_hash,
)

if TYPE_CHECKING:
    from collections.abc import Callable

# ---------------------------------------------------------------------------
# Projection constants
# ---------------------------------------------------------------------------

#: Marker embedded in every GitHub comment projection.  Used to locate the
#: document block when parsing a raw issue comment.
GITHUB_MARKER: str = "<!-- bernstein-volunteer-doc -->"

#: Prefix for base64-encoded body (used when the raw dict contains characters
#: that would confuse markdown rendering).
GITHUB_BASE64_PREFIX: str = "base64:"


# ---------------------------------------------------------------------------
# Hub projections
# ---------------------------------------------------------------------------


def to_hub_projection(doc: dict[str, Any]) -> bytes:
    """Project a document to the hub (plain JSON bytes).

    This is the simplest projection: canonical JSON bytes, no wrapper.
    Any downstream consumer reads it with ``json.loads``.

    Args:
        doc: A canonical-ready dict (typically produced by
            ``to_canonical_dict`` on a document dataclass).

    Returns:
        UTF-8 encoded JSON bytes.
    """
    return canonical_bytes(doc)


def from_hub_projection(raw: bytes | dict[str, Any]) -> dict[str, Any]:
    """Parse a hub projection back into a canonical dict.

    Accepts either raw JSON bytes (the common case when reading from a file
    or webhook payload) or an already-parsed dict (useful for chaining).

    Args:
        raw: UTF-8 JSON bytes or an already-parsed dict.

    Returns:
        The canonical dict recovered from the projection.

    Raises:
        ValueError: If ``raw`` is bytes that are not valid UTF-8 JSON.
    """
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, bytes):
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"hub projection is not valid UTF-8 JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError(f"hub projection is a JSON {type(parsed).__name__}, expected object")
        return parsed
    raise ValueError(f"hub projection must be bytes or dict, got {type(raw).__name__}")


# ---------------------------------------------------------------------------
# GitHub projections
# ---------------------------------------------------------------------------


def to_github_projection(doc: dict[str, Any]) -> str:
    """Project a document to a GitHub issue comment string.

    Format::

        <!-- bernstein-volunteer-doc -->
        ```json
        <canonical JSON of doc>
        ```

    The fenced block is human-readable and copy-pasteable.  If the document
    contains characters that would break a fenced block (e.g. a backtick inside
    the JSON), the block is base64-encoded and prefixed with ``base64:``.

    Args:
        doc: A canonical-ready dict.

    Returns:
        A multi-line string suitable for posting as a GitHub issue comment.
    """
    json_bytes = canonical_bytes(doc)

    # Detect characters that would break a fenced block.
    problematic = b"```" in json_bytes
    if problematic:
        encoded = base64.b64encode(json_bytes).decode("ascii")
        body = f"{GITHUB_BASE64_PREFIX}{encoded}"
    else:
        body = json_bytes.decode("utf-8")

    return f"{GITHUB_MARKER}\n```json\n{body}\n```"


def from_github_projection(raw: str | dict[str, Any]) -> dict[str, Any]:
    """Parse a GitHub issue comment back into a canonical dict.

    Accepts either a raw comment string (as returned by the GitHub API) or
    an already-parsed dict (useful for testing without the wrapper).

    Extraction algorithm::

        1. Strip the leading ``<!-- bernstein-volunteer-doc -->`` marker.
        2. Strip the opening `````json`` and closing ``````` fence markers.
        3. If the remaining body starts with ``base64:``, decode it.
        4. ``json.loads`` the result.

    Args:
        raw: A raw GitHub issue comment string or an already-parsed dict.

    Returns:
        The canonical dict recovered from the projection.

    Raises:
        ValueError: If the string does not contain the marker or the JSON is
            malformed.
    """
    if isinstance(raw, dict):
        return dict(raw)

    if not isinstance(raw, str):
        raise ValueError(f"GitHub projection must be str or dict, got {type(raw).__name__}")

    # Strip the marker line.
    marker_line = GITHUB_MARKER.strip()
    if marker_line not in raw:
        raise ValueError(f"GitHub comment does not contain marker {GITHUB_MARKER!r}")

    content = raw.split(marker_line, 1)[1]

    # Strip fence markers (look for ``` on its own line).
    fence_open = "```json"
    fence_close = "```"
    if fence_open not in content:
        raise ValueError("GitHub comment is missing ```json fence")
    after_open = content.split(fence_open, 1)[1]
    if fence_close not in after_open:
        raise ValueError("GitHub comment is missing closing ``` fence")
    json_str = after_open.split(fence_close, 1)[0]

    json_str = json_str.strip()

    # Base64 decode if prefixed.
    if json_str.startswith(GITHUB_BASE64_PREFIX):
        b64_body = json_str[len(GITHUB_BASE64_PREFIX) :]
        try:
            json_bytes = base64.b64decode(b64_body)
        except Exception as exc:
            raise ValueError(f"GitHub projection base64 decode failed: {exc}") from exc
    else:
        try:
            json_bytes = json_str.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError(f"GitHub projection is not valid UTF-8: {exc}") from exc

    try:
        parsed = json.loads(json_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"GitHub projection contains invalid JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise ValueError(f"GitHub projection recovered a JSON {type(parsed).__name__}, expected object")

    return parsed


# ---------------------------------------------------------------------------
# Generic conformance harness
# ---------------------------------------------------------------------------

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class ConformanceResult:
    """Outcome of a single conformance check.

    Attributes:
        ok: True iff all projection round-trips reproduced the same canonical hash.
        doc_type: Name of the document type checked (used in error messages).
        original_hash: Canonical hash of the original dict.
        github_hash: Canonical hash after GitHub round-trip.
        hub_hash: Canonical hash after hub round-trip.
        error: Human-readable error message (empty when ``ok``).
    """

    ok: bool
    doc_type: str = ""
    original_hash: str = ""
    github_hash: str = ""
    hub_hash: str = ""
    error: str = ""


@dataclass(frozen=True, slots=True)
class ConformanceHarness:
    """Generic conformance checker for volunteer protocol documents.

    Register one or more document types with ``register()``, then call
    ``check()`` on each.  The harness proves that every registered type
    survives the GitHub and hub projections with its canonical hash intact.

    Example::

        harness = ConformanceHarness()

        def claim_to_dict(claim) -> dict[str, Any]:
            return claim.to_canonical_dict()

        def dict_to_claim(d: dict) -> dict[str, Any]:
            return d  # dict already

        harness.register(
            name="Claim",
            to_canonical_dict=claim_to_dict,
            from_canonical_dict=dict_to_claim,
        )

        result = harness.check("Claim", {"worker_id": "w1", "task_id": "t1", "claimed_at": "2024-01-01T00:00:00+00:00"})
        assert result.ok
    """

    _schemas: dict[str, tuple[Callable[[Any], dict[str, Any]], Callable[[dict[str, Any]], Any]]] = field(
        default_factory=dict
    )

    def register(
        self,
        *,
        name: str,
        to_canonical_dict: Callable[[Any], dict[str, Any]],
        from_canonical_dict: Callable[[dict[str, Any]], Any],
    ) -> None:
        """Register a document type with the harness.

        Args:
            name: Human-readable name for this document type (used in results).
            to_canonical_dict: A callable that returns the canonical dict
                representation of a document instance.
            from_canonical_dict: A callable that accepts a canonical dict and
                returns a document instance (or the dict itself).
        """
        self._schemas[name] = (to_canonical_dict, from_canonical_dict)

    def check(self, doc_type: str, doc: Any) -> ConformanceResult:
        """Check conformance for a single document.

        Verifies that the canonical hash is stable through both projections.

        Args:
            doc_type: Name registered with ``register()``.
            doc: A document instance (type must have been registered).

        Returns:
            :class:`ConformanceResult` describing the outcome.
        """
        if doc_type not in self._schemas:
            return ConformanceResult(
                ok=False,
                doc_type=doc_type,
                error=f"unknown document type {doc_type!r}; register it first with harness.register()",
            )

        to_canonical, _from_canonical = self._schemas[doc_type]

        try:
            canonical = to_canonical(doc)
        except Exception as exc:
            return ConformanceResult(
                ok=False,
                doc_type=doc_type,
                error=f"to_canonical_dict raised {type(exc).__name__}: {exc}",
            )

        if not isinstance(canonical, dict):
            return ConformanceResult(
                ok=False,
                doc_type=doc_type,
                error=f"to_canonical_dict returned {type(canonical).__name__}, expected dict",
            )

        original_hash = canonical_hash(canonical)

        # GitHub round-trip.
        github_proj = to_github_projection(canonical)
        try:
            github_parsed = from_github_projection(github_proj)
        except Exception as exc:
            return ConformanceResult(
                ok=False,
                doc_type=doc_type,
                original_hash=original_hash,
                error=f"GitHub round-trip raised {type(exc).__name__}: {exc}",
            )
        github_hash = canonical_hash(github_parsed)

        # Hub round-trip.
        hub_proj = to_hub_projection(canonical)
        try:
            hub_parsed = from_hub_projection(hub_proj)
        except Exception as exc:
            return ConformanceResult(
                ok=False,
                doc_type=doc_type,
                original_hash=original_hash,
                github_hash=github_hash,
                error=f"Hub round-trip raised {type(exc).__name__}: {exc}",
            )
        hub_hash = canonical_hash(hub_parsed)

        # Both round-trips must recover the same canonical hash.
        if github_hash != original_hash:
            return ConformanceResult(
                ok=False,
                doc_type=doc_type,
                original_hash=original_hash,
                github_hash=github_hash,
                hub_hash=hub_hash,
                error=f"GitHub round-trip hash mismatch: {github_hash[:16]}… != {original_hash[:16]}…",
            )
        if hub_hash != original_hash:
            return ConformanceResult(
                ok=False,
                doc_type=doc_type,
                original_hash=original_hash,
                github_hash=github_hash,
                hub_hash=hub_hash,
                error=f"Hub round-trip hash mismatch: {hub_hash[:16]}… != {original_hash[:16]}…",
            )

        return ConformanceResult(
            ok=True,
            doc_type=doc_type,
            original_hash=original_hash,
            github_hash=github_hash,
            hub_hash=hub_hash,
        )


# ---------------------------------------------------------------------------
# Convenience: assert_conformance
# ---------------------------------------------------------------------------


def assert_conformance(
    doc: Any,
    *,
    harness: ConformanceHarness | None = None,
    name: str = "",
    to_canonical_dict: Callable[[Any], dict[str, Any]] | None = None,
    from_canonical_dict: Callable[[dict[str, Any]], Any] | None = None,
) -> ConformanceResult:
    """Assert that a document conforms to both projections.

    A thin wrapper around :class:`ConformanceHarness` that raises
    ``AssertionError`` on failure.  Suitable for use in tests.

    When ``harness`` is supplied, only the name is used to look up the
    registered document type — the harness is shared so callers can
    pre-register multiple types and verify one of them without creating a
    fresh harness each time.  When ``harness`` is omitted, a new harness
    is created and the document type is registered on-the-fly from
    ``to_canonical_dict`` / ``from_canonical_dict``.

    Args:
        doc: A document instance.
        harness: Optional pre-built harness; when supplied the name must
            already be registered on it.
        name: Human-readable name for the document type.  Ignored when
            ``harness`` is provided and the type is already registered.
        to_canonical_dict: A callable that returns the canonical dict.
            Required when ``harness`` is omitted.
        from_canonical_dict: A callable that accepts a canonical dict.
            Required when ``harness`` is omitted.

    Returns:
        :class:`ConformanceResult` on success.

    Raises:
        AssertionError: If either projection round-trip fails.
    """
    if harness is None:
        if not name or to_canonical_dict is None or from_canonical_dict is None:
            raise ValueError("assert_conformance requires name + to/from_canonical_dict when harness is None")
        harness = ConformanceHarness()
        harness.register(name=name, to_canonical_dict=to_canonical_dict, from_canonical_dict=from_canonical_dict)
    result = harness.check(name, doc)
    if not result.ok:
        raise AssertionError(result.error)
    return result
