"""Engine-agnostic normalised query result + canonical byte encoding.

The whole point of a query receipt is that *the exact bytes the model saw*
have a single, stable digest. That digest cannot depend on which engine ran
the query, only on the logical result: an ordered schema (column name + type)
and ordered rows of typed values. This module defines that normalised shape
(:class:`NormalizedResult`) and the deterministic byte encoding
(:func:`canonical_bytes`) whose SHA-256 is the ``content_hash``.

Design notes
------------

* **Engine-agnostic.** :class:`NormalizedResult` carries only Python-native
  canonical types (see :data:`CANONICAL_TYPES`). Any engine adapter -- the
  stdlib-``sqlite3`` reference engine, an in-memory reference engine, or a
  future Arrow-interchange warehouse adapter -- is responsible for mapping its
  own type system onto these. Two engines that produce the same logical result
  therefore produce byte-identical canonical output.

* **Unambiguous framing.** Every field is length-prefixed (netstring-style:
  ``<len>:<bytes>``) so a text value containing a delimiter, a newline, or the
  literal word ``NULL`` can never be confused with structure. Each cell also
  carries a one-byte type tag, so a NULL (tag ``n``) can never collide with the
  empty string (tag ``t``).

* **Truncation is inside the hash.** The truncation flag and the row cap are
  part of the hashed body, so a truncated result can never re-hash to the same
  digest as the full result it was cut from.

* **Fail-closed text.** Text is required to already be NFC. The encoder never
  silently normalises -- see :class:`NonCanonicalText`.

Per-type rendering rules (fixed, versioned by :data:`_MAGIC`):

===========  ==================================================================
Canonical    Rendering
===========  ==================================================================
integer      exact base-10 (``str(int)``), tag ``i``
decimal      fixed-point, scale preserved (``format(Decimal, "f")``), tag ``d``
float        shortest round-trip (``repr(float)``), tag ``f``
boolean      ``1`` / ``0``, tag ``o``
text         NFC UTF-8 bytes (rejected if not NFC), tag ``t``
blob         raw bytes, tag ``b``
null         empty, tag ``n`` (applies to any column type)
===========  ==================================================================
"""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass
from decimal import Decimal

from bernstein.core.datasources.errors import NonCanonicalText, UnsupportedValue

#: Canonical column type names. A column declares exactly one of these (in
#: select order); individual cells may still be NULL regardless of the
#: column's declared type.
INTEGER = "integer"
DECIMAL = "decimal"
FLOAT = "float"
BOOLEAN = "boolean"
TEXT = "text"
BLOB = "blob"

CANONICAL_TYPES: frozenset[str] = frozenset({INTEGER, DECIMAL, FLOAT, BOOLEAN, TEXT, BLOB})

#: Format tag. Bump this if the rendering rules ever change so an old receipt
#: can never silently re-hash under new rules.
_MAGIC = b"bqr\x01"

# Per-canonical-type cell tags. One byte, first byte of every rendered cell.
_TAG_INTEGER = b"i"
_TAG_DECIMAL = b"d"
_TAG_FLOAT = b"f"
_TAG_BOOLEAN = b"o"
_TAG_TEXT = b"t"
_TAG_BLOB = b"b"
_TAG_NULL = b"n"


@dataclass(frozen=True, slots=True)
class NormalizedColumn:
    """One column of a normalised result: a name and a canonical type."""

    name: str
    type: str

    def __post_init__(self) -> None:
        if self.type not in CANONICAL_TYPES:
            raise UnsupportedValue(f"unknown canonical column type: {self.type!r}")


@dataclass(frozen=True, slots=True)
class NormalizedResult:
    """An engine-agnostic query result ready for canonical encoding.

    Attributes:
        columns: Column schema in select order.
        rows: Rows in result order. Each row is a tuple aligned to ``columns``;
            each cell is a canonical Python value (``int``, ``Decimal``,
            ``float``, ``bool``, ``str``, ``bytes``) or ``None`` for SQL NULL.
        truncated: True when a row cap cut the result short.
        row_cap: The row cap that was in force (0 means "no cap applied").
    """

    columns: tuple[NormalizedColumn, ...]
    rows: tuple[tuple[object, ...], ...]
    truncated: bool = False
    row_cap: int = 0

    @property
    def row_count(self) -> int:
        return len(self.rows)


def _nfc_bytes(s: str, *, what: str) -> bytes:
    """UTF-8 bytes of ``s``, rejecting any string that is not already NFC."""
    if unicodedata.normalize("NFC", s) != s:
        raise NonCanonicalText(f"{what} is not NFC-normalised: {s!r}")
    return s.encode("utf-8")


def _field(payload: bytes) -> bytes:
    """Length-prefix ``payload`` netstring-style: ``<len>:<payload>``."""
    return str(len(payload)).encode("ascii") + b":" + payload


def _render_cell(col_type: str, value: object) -> bytes:
    """Render a single cell to its tagged canonical bytes.

    The column's declared type is advisory: the *value's* Python type decides
    the rendering, so a NULL in an integer column renders as the NULL sentinel
    rather than a bogus integer. A value whose Python type is incompatible with
    any canonical rendering raises :class:`UnsupportedValue`.
    """
    if value is None:
        return _TAG_NULL
    # ``bool`` is a subclass of ``int`` -- check it first so True/False never
    # render as 1/0 integers by accident.
    if isinstance(value, bool):
        return _TAG_BOOLEAN + (b"1" if value else b"0")
    if isinstance(value, int):
        return _TAG_INTEGER + str(value).encode("ascii")
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise UnsupportedValue(f"non-finite Decimal in {col_type} column: {value!r}")
        # ``format(d, "f")`` is fixed-point and preserves scale (trailing
        # zeros), so 1.50 and 1.5 are distinct canonical renderings.
        return _TAG_DECIMAL + format(value, "f").encode("ascii")
    if isinstance(value, float):
        # ``repr`` is the shortest string that round-trips to the same float
        # (CPython >= 3.1). inf/-inf/nan render as 'inf'/'-inf'/'nan'.
        return _TAG_FLOAT + repr(value).encode("ascii")
    if isinstance(value, str):
        return _TAG_TEXT + _nfc_bytes(value, what="text value")
    if isinstance(value, (bytes, bytearray)):
        return _TAG_BLOB + bytes(value)
    raise UnsupportedValue(f"cannot canonically render value of type {type(value).__name__}: {value!r}")


def canonical_bytes(result: NormalizedResult) -> bytes:
    """Return the deterministic canonical byte encoding of ``result``.

    Byte-identical for any two engines that produced the same logical result.
    The truncation flag and row cap are part of the encoded body so a truncated
    result can never share a digest with the untruncated original.
    """
    parts: list[bytes] = [_MAGIC]
    parts.append(_field(b"truncated=" + (b"1" if result.truncated else b"0")))
    parts.append(_field(b"row_cap=" + str(int(result.row_cap)).encode("ascii")))
    parts.append(_field(b"cols=" + str(len(result.columns)).encode("ascii")))
    parts.append(_field(b"rows=" + str(len(result.rows)).encode("ascii")))
    for col in result.columns:
        parts.append(_field(b"C"))
        parts.append(_field(_nfc_bytes(col.name, what="column name")))
        parts.append(_field(col.type.encode("ascii")))
    for row in result.rows:
        if len(row) != len(result.columns):
            raise UnsupportedValue(f"row arity {len(row)} does not match column count {len(result.columns)}")
        parts.append(_field(b"R"))
        for col, value in zip(result.columns, row, strict=True):
            parts.append(_field(_render_cell(col.type, value)))
    return b"".join(parts)


def content_hash(result: NormalizedResult) -> str:
    """Return ``sha256:<hex>`` over :func:`canonical_bytes` of ``result``."""
    return "sha256:" + hashlib.sha256(canonical_bytes(result)).hexdigest()


def _render_scalar(value: object) -> str:
    """Human/agent-facing rendering of one cell (distinct from the hash form)."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, (bytes, bytearray)):
        return "0x" + bytes(value).hex()
    return str(value)


def render_text(result: NormalizedResult) -> str:
    """Render ``result`` as a stable text block for injection into a prompt.

    This is the text an agent reads; the receipt attests the canonical bytes
    behind it. The rendering is deterministic (pipe-delimited, no borders) so it
    stays diff-stable, but it is *not* the hashed form -- ``content_hash`` always
    derives from :func:`canonical_bytes`, never from this text.
    """
    header = " | ".join(f"{c.name} ({c.type})" for c in result.columns)
    lines = [header]
    for row in result.rows:
        lines.append(" | ".join(_render_scalar(v) for v in row))
    footer = f"({result.row_count} row{'s' if result.row_count != 1 else ''}"
    if result.truncated:
        footer += f", truncated at row_cap={result.row_cap}"
    footer += ")"
    lines.append(footer)
    return "\n".join(lines)


__all__ = [
    "BLOB",
    "BOOLEAN",
    "CANONICAL_TYPES",
    "DECIMAL",
    "FLOAT",
    "INTEGER",
    "TEXT",
    "NormalizedColumn",
    "NormalizedResult",
    "canonical_bytes",
    "content_hash",
    "render_text",
]
