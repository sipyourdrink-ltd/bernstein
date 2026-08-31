"""Versioned replay-key derivation for the record/replay gateway.

Callers still pass opaque strings into :class:`~bernstein.core.replay.gateway.ReplayGateway`;
the gateway writes and looks up scheme-prefixed digests (``v1:<64 hex>``) so a
future derivation change can classify an old corpus as "recorded under an
older scheme" instead of reporting false divergences (#4867).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from typing import Final

#: Scheme used for new recordings. Bump only when adding a new ``_digest_vN``.
CURRENT_KEY_SCHEME: Final = "v1"

#: Fixed fractional digits so ``600`` and ``600.0`` share one numeric identity.
_NUMERIC_PRECISION: Final = 10

_STORED_KEY_RE = re.compile(r"\A(v[0-9]+):([0-9a-f]{64})\Z")


def parse_stored_key(stored: str) -> tuple[str | None, str]:
    """Split a stored key into ``(scheme, digest_or_raw)``.

    Returns ``(None, stored)`` when the value is not a scheme-prefixed digest
    (legacy unversioned rows).
    """
    match = _STORED_KEY_RE.fullmatch(stored)
    if match is None:
        return None, stored
    return match.group(1), match.group(2)


def derive_replay_key(*components: object, scheme: str | None = None) -> str:
    """Return ``{scheme}:<64 hex>`` for ``components`` under ``scheme``.

    Each scheme keeps its own digest function for as long as recordings under
    it may exist. Unknown schemes raise :class:`ValueError`.
    """
    scheme_id = CURRENT_KEY_SCHEME if scheme is None else scheme
    if scheme_id == "v1":
        digest = _digest_v1(components)
    elif scheme_id == "v2":
        digest = _digest_v2(components)
    else:
        raise ValueError(f"unknown replay key scheme {scheme_id!r}")
    return f"{scheme_id}:{digest}"


def _digest_v1(components: tuple[object, ...]) -> str:
    payload = _encode_v1(components)
    return hashlib.sha256(b"bernstein.replay.key.v1\0" + payload).hexdigest()


def _digest_v2(components: tuple[object, ...]) -> str:
    """Next scheme: same component encoding, distinct domain separator.

    Kept so a v1 corpus replayed by a v2 verifier is classifiable (#4867).
    Production recordings stay on :data:`CURRENT_KEY_SCHEME` until deliberately
    bumped.
    """
    payload = _encode_v1(components)
    return hashlib.sha256(b"bernstein.replay.key.v2\0" + payload).hexdigest()


def _u32(n: int) -> bytes:
    if n < 0 or n >= 2**32:
        raise ValueError("length out of range for replay key encoding")
    return n.to_bytes(4, "big")


def _encode_v1(value: object) -> bytes:
    """Canonical byte encoding for scheme v1 (and v2's shared payload).

    Tags distinguish ``None`` from ``""``. Integers and floats share a fixed-
    precision decimal form. Sequences are count- and length-prefixed so an
    element's content cannot forge a component boundary.
    """
    if value is None:
        return b"\x00"
    if isinstance(value, bool):
        # ``bool`` is an ``int`` subclass; pin before the numeric branch.
        return b"\x01" + (b"\x01" if value else b"\x00")
    if isinstance(value, (int, float)):
        body = format(float(value), f".{_NUMERIC_PRECISION}f").encode("ascii")
        return b"\x02" + _u32(len(body)) + body
    if isinstance(value, str):
        body = value.encode("utf-8")
        return b"\x03" + _u32(len(body)) + body
    if isinstance(value, (bytes, bytearray, memoryview)):
        body = bytes(value)
        return b"\x04" + _u32(len(body)) + body
    if isinstance(value, Sequence):
        parts = [_encode_v1(item) for item in value]
        out = bytearray(b"\x05")
        out += _u32(len(parts))
        for part in parts:
            out += _u32(len(part))
            out += part
        return bytes(out)
    body = repr(value).encode("utf-8", errors="replace")
    return b"\xff" + _u32(len(body)) + body
