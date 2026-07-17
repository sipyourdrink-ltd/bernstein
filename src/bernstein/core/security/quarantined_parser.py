"""Quarantined structural parsing for untrusted payloads (issue #2513).

Untrusted tool results (a hostile web page, a crafted issue body, a
third-party MCP result) must never flow verbatim into worker prompt context:
their free text is exactly where prompt-injection lives. This module extracts
only schema-validated *structural* fields -- integers, enums, slug lists --
and withholds every free-text field, representing it by a content hash so the
extracted artefact can still carry a lineage edge back to the tainted source.

The extractor never returns arbitrary free text. A field declared ``opaque``
(a title, an issue body) is dropped from the emitted fields and surfaced only
as ``<name>_sha256`` plus ``<name>_len``. There is no code path by which an
instruction-bearing string inside the payload becomes a value in
``QuarantinedExtract.fields``.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = [
    "FieldSpec",
    "QuarantinedExtract",
    "content_hash_of",
    "extract_structured",
]

# A slug is a lowercase run of alphanumerics plus ``-``, ``_`` and ``.`` -- the
# character set of a label, a git ref, or a login. Anything else is stripped,
# so a value like ``../etc/passwd`` or ``x; rm -rf /`` cannot survive as-is.
_SLUG_STRIP_RE = re.compile(r"[^a-z0-9._-]+")

# A concretely-typed empty allow-set so the FieldSpec default is not inferred
# as ``frozenset[Unknown]`` under strict type checking.
_NO_ALLOWED: frozenset[str] = frozenset()


def content_hash_of(payload: bytes | str) -> str:
    """Return ``sha256:<hex>`` of *payload* (UTF-8 for ``str``)."""
    data = payload.encode("utf-8") if isinstance(payload, str) else payload
    return "sha256:" + hashlib.sha256(data).hexdigest()


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """Declares how one field is extracted from an untrusted payload.

    Attributes:
        kind: One of ``int``, ``enum``, ``slug``, ``slug_list``, ``opaque``.
            ``opaque`` fields are withheld and represented by a hash only.
        max_len: Maximum length for slug values (longer values are dropped).
        max_items: Maximum items for ``slug_list``.
        allowed: Permitted values for ``enum`` (anything else is dropped).
    """

    kind: str
    max_len: int = 256
    max_items: int = 64
    allowed: frozenset[str] = field(default_factory=lambda: _NO_ALLOWED)


@dataclass(frozen=True, slots=True)
class QuarantinedExtract:
    """Result of a quarantined extraction.

    Attributes:
        fields: Schema-validated structural values only. Free text never
            appears here; withheld fields contribute ``<name>_sha256`` and
            ``<name>_len`` entries instead.
        source_content_hash: ``sha256:<hex>`` of the raw payload bytes, the
            anchor for the lineage edge back to the tainted source.
        withheld: Names of fields whose raw value was withheld (free text or a
            value that failed validation).
    """

    fields: dict[str, Any]
    source_content_hash: str
    withheld: tuple[str, ...]


def _extract_int(value: object) -> int | None:
    # Strict: only a genuine int or an all-digit string. ``"42; rm -rf /"``
    # must not parse. Booleans are ints in Python but are not valid here.
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        return int(value.strip())
    return None


def _extract_slug(value: object, max_len: int) -> str | None:
    if not isinstance(value, str):
        return None
    slug = _SLUG_STRIP_RE.sub("-", value.strip().lower()).strip("-._")
    if not slug or len(slug) > max_len:
        return None
    return slug


def _extract_enum(value: object, allowed: frozenset[str]) -> str | None:
    if isinstance(value, str) and value in allowed:
        return value
    return None


def _extract_slug_list(value: object, spec: FieldSpec) -> tuple[str, ...] | None:
    if not isinstance(value, (list, tuple)):
        return None
    items = cast("Sequence[object]", value)
    out: list[str] = []
    for item in items[: spec.max_items]:
        slug = _extract_slug(item, spec.max_len)
        if slug is not None:
            out.append(slug)
    return tuple(out)


def _coerce_source_bytes(payload: Mapping[str, Any] | str, source_bytes: bytes | None) -> bytes:
    if source_bytes is not None:
        return source_bytes
    if isinstance(payload, str):
        return payload.encode("utf-8")
    return json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")


def extract_structured(
    payload: Mapping[str, Any] | str,
    schema: Mapping[str, FieldSpec],
    *,
    source_bytes: bytes | None = None,
) -> QuarantinedExtract:
    """Extract schema-validated structural fields from an untrusted *payload*.

    A raw ``str`` payload is treated as a single ``body`` value (the schema
    should declare ``body`` as ``opaque``). Every field is validated against
    its :class:`FieldSpec`; anything that is free text or fails validation is
    withheld, never emitted verbatim.

    Args:
        payload: The untrusted structured payload (or a raw string body).
        schema: Field name -> :class:`FieldSpec`.
        source_bytes: Optional exact source bytes to anchor the lineage edge;
            derived from ``payload`` when omitted.

    Returns:
        A :class:`QuarantinedExtract`.
    """
    values: Mapping[str, Any]
    if isinstance(payload, str):
        # The whole payload is one free-text body; only an ``opaque`` schema
        # field can consume it, and it will be withheld.
        body_key = next((k for k, s in schema.items() if s.kind == "opaque"), "body")
        values = {body_key: payload}
    else:
        values = payload

    out_fields: dict[str, Any] = {}
    withheld: list[str] = []

    for name, spec in schema.items():
        if name not in values:
            continue
        raw = values[name]

        if spec.kind == "opaque":
            # Never emit free text. Represent it by a hash + length only.
            text = raw if isinstance(raw, str) else json.dumps(raw, sort_keys=True, ensure_ascii=False)
            out_fields[f"{name}_sha256"] = content_hash_of(text)
            out_fields[f"{name}_len"] = len(text)
            withheld.append(name)
            continue

        extracted: Any
        if spec.kind == "int":
            extracted = _extract_int(raw)
        elif spec.kind == "enum":
            extracted = _extract_enum(raw, spec.allowed)
        elif spec.kind == "slug":
            extracted = _extract_slug(raw, spec.max_len)
        elif spec.kind == "slug_list":
            extracted = _extract_slug_list(raw, spec)
        else:
            extracted = None

        if extracted is None:
            withheld.append(name)
        else:
            out_fields[name] = extracted

    return QuarantinedExtract(
        fields=out_fields,
        source_content_hash=content_hash_of(_coerce_source_bytes(payload, source_bytes)),
        withheld=tuple(withheld),
    )
