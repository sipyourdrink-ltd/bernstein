"""Lineage entry schema + RFC 8785 JCS canonicalisation.

A `LineageEntry` is a single immutable record of an agent writing an artefact.
The canonical-bytes form (RFC 8785 JCS) is what gets HMAC'd and Ed25519-signed,
so every entry has a stable wire-form regardless of how it's reconstructed.
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import json
from dataclasses import asdict, dataclass

LINEAGE_ENTRY_VERSION = 1

# Closed set of recordable artefact kinds. The coding path records file writes
# as ``file``; the non-coding artifact contract (issue #2608) adds the four
# activity-worker output kinds (``report`` / ``dataset`` / ``action_log`` /
# ``ops_result``) so a non-coding artifact can be a first-class signed lineage
# record. The set stays closed - membership only widens and an unknown kind
# still raises in :meth:`LineageEntry.__post_init__`.
ARTEFACT_KINDS: frozenset[str] = frozenset(
    {
        "file",
        "sdd-runtime",
        "mcp-result",
        "config",
        "tool-result",
        "report",
        "dataset",
        "action_log",
        "ops_result",
    }
)

#: Provenance trust classes recordable on an entry (issue #2513). ``None`` on
#: an entry means "no trust class recorded" and is dropped from the canonical
#: form so pre-feature entries keep byte-identical wire bytes. Taint projection
#: treats absence as the lowest trust class (fail closed) -- see
#: :mod:`bernstein.core.lineage.provenance`.
TRUST_CLASSES: frozenset[str] = frozenset({"operator", "workspace", "first_party", "third_party", "public"})


@dataclass(frozen=True, slots=True)
class LineageEntry:
    """Single lineage event.

    Frozen + slots so the dataclass shape itself is canonical - no surprise
    extra attributes can mutate the byte form.
    """

    v: int
    artefact_path: str
    artefact_kind: str
    content_hash: str
    parent_hashes: list[str]
    agent_id: str
    agent_card_kid: str
    tool_call_id: str
    span_id: str
    ts_ns: int
    operator_hmac: str
    # Additive, optional (issue #2513). ``None`` is dropped from the canonical
    # bytes so every historical entry keeps its exact wire form, signature and
    # HMAC. A tool-result provenance record sets this to one of TRUST_CLASSES.
    trust_class: str | None = None

    def __post_init__(self) -> None:
        if self.v != LINEAGE_ENTRY_VERSION:
            raise ValueError(f"unsupported entry version: {self.v}")
        if self.artefact_kind not in ARTEFACT_KINDS:
            raise ValueError(f"unknown artefact_kind: {self.artefact_kind!r}")
        if not self.content_hash.startswith("sha256:"):
            raise ValueError(f"content_hash must start with 'sha256:', got {self.content_hash!r}")
        for p in self.parent_hashes:
            if not p.startswith("sha256:"):
                raise ValueError(f"parent_hash must start with 'sha256:', got {p!r}")
        if self.trust_class is not None and self.trust_class not in TRUST_CLASSES:
            raise ValueError(f"unknown trust_class: {self.trust_class!r}")


def _canonical_body(entry: LineageEntry) -> dict[str, object]:
    """Return the entry as a plain dict ready for JCS canonicalisation.

    The optional ``trust_class`` field is dropped when ``None`` so that an
    entry that carries no trust class canonicalises byte-for-byte identically
    to the pre-issue-#2513 schema. This keeps every historical signature,
    HMAC and golden snapshot valid while letting provenance records add the
    field additively. Both :func:`canonicalise` and
    :func:`compute_operator_hmac` route through here so the two paths can
    never diverge on the drop rule (see ADR-009 §5.2).
    """
    body = asdict(entry)
    if body.get("trust_class") is None:
        body.pop("trust_class", None)
    return body


def canonicalise(entry: LineageEntry) -> bytes:
    """RFC 8785 JSON Canonicalisation Scheme.

    sort_keys=True + minimal separators + UTF-8 covers the subset relevant to
    flat objects of strings / ints / lists-of-strings. We never put floats,
    None, or nested objects into a LineageEntry, so the corner cases of RFC
    8785 around ES6 number formatting and recursive ordering don't apply.
    """
    return json.dumps(
        _canonical_body(entry),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def entry_hash(entry: LineageEntry) -> str:
    return "sha256:" + hashlib.sha256(canonicalise(entry)).hexdigest()


def compute_operator_hmac(entry: LineageEntry, key: bytes) -> str:
    """Return the canonical operator-HMAC for ``entry`` under ``key``.

    The HMAC covers the JCS-canonical bytes of the entry with the
    ``operator_hmac`` field replaced by the empty string. This binds every
    field of the entry (including ``agent_id``, ``artefact_path`` and the
    full ``parent_hashes`` list), so any post-signing substitution attack is
    independently detected by both the JWS and the HMAC envelope.

    Recorder and gate share this helper so on-disk entries are accepted by
    the CI gate when the operator secret is supplied. Any divergence between
    the two paths would silently invalidate every entry - see ADR-009 §5.2.
    """
    body = _canonical_body(entry)
    body["operator_hmac"] = ""
    canonical = json.dumps(body, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return _hmac.new(key, canonical, hashlib.sha256).hexdigest()
