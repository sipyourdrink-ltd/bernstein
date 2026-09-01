"""The one canonical-JSON byte rule for hashing and signing.

Every signature or content hash in this package is computed over bytes, and
the bytes are derived from a JSON-shaped payload. When two modules derive
those bytes differently, a signature made under one cannot verify under the
other, and the artefact alone does not say which rule produced it. This
module owns the rule so that there is exactly one answer.

The rule (``CANONICALIZATION_VERSION`` 1): keys sorted, minimal separators,
UTF-8 with non-ASCII characters preserved, NaN and infinities refused because
they are not JSON. Two operators canonicalising the same payload get the same
bytes on any platform.

The two ``legacy_*`` encoders reproduce the byte rules that signed artefacts
before this module existed. A verifier that meets an artefact with no
canonicalization version selects the legacy rule its producer used, instead
of guessing; see :func:`bytes_for_verification`.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

CANONICALIZATION_VERSION = 1
"""The byte rule :func:`canonical_bytes` implements. Signed artefacts record it."""

CANONICALIZATION_FIELD = "canonicalization"
"""Field name a signed artefact uses to carry :data:`CANONICALIZATION_VERSION`."""


class UnsupportedCanonicalization(ValueError):
    """The artefact names a canonicalization version this build cannot reproduce."""


def canonical_bytes(payload: Any) -> bytes:
    """Return the canonical JSON bytes of ``payload`` (version 1).

    Sorted keys, ``(",", ":")`` separators, UTF-8 with non-ASCII preserved,
    ``allow_nan=False`` so a payload that is not representable as JSON raises
    here rather than producing bytes no strict parser accepts.
    """
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")


def legacy_ascii_bytes(payload: Any) -> bytes:
    """Byte rule of the producers that omitted ``ensure_ascii=False``.

    Identical to :func:`canonical_bytes` for ASCII-only payloads; escapes
    non-ASCII characters as ``\\uXXXX`` otherwise. Kept only so artefacts
    signed under that rule keep verifying.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def legacy_pretty_bytes(payload: Any) -> bytes:
    """Byte rule of the producers that wrote indented, ASCII-escaped JSON.

    Two-space indentation and a trailing newline. Kept only so artefacts
    hashed under that rule keep verifying.
    """
    return (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8")


def bytes_for_verification(
    payload: Any,
    version: int | None,
    *,
    legacy: Callable[[Any], bytes],
) -> bytes:
    """Return the bytes a verifier must recompute for a stored artefact.

    ``version`` is the value the artefact carries under
    :data:`CANONICALIZATION_FIELD`; ``None`` means the artefact predates the
    field and was produced under ``legacy``, the rule its producer used at
    the time. Any other version this build does not implement raises
    :class:`UnsupportedCanonicalization` so the verifier reports the gap
    instead of returning a mismatch that reads as tampering.
    """
    if version is None:
        return legacy(payload)
    if version == CANONICALIZATION_VERSION:
        return canonical_bytes(payload)
    raise UnsupportedCanonicalization(
        f"canonicalization version {version!r} is not implemented by this build (supports {CANONICALIZATION_VERSION})"
    )


__all__ = [
    "CANONICALIZATION_FIELD",
    "CANONICALIZATION_VERSION",
    "UnsupportedCanonicalization",
    "bytes_for_verification",
    "canonical_bytes",
    "legacy_ascii_bytes",
    "legacy_pretty_bytes",
]
