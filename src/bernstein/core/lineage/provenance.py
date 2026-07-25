"""Provenance trust classes with lineage-propagated taint (issue #2513).

Every tool result is recorded as a content-addressed lineage entry carrying a
``trust_class``. The *effective* trust of any artefact is the minimum trust
class over its lineage closure -- a deterministic projection of the signed
lineage graph, recomputable offline by any verifier holding the log.

The label is not metadata bolted onto a boolean: it lives inside the signed,
HMAC-chained lineage entry, and the verdict is a pure function of the graph's
``parent_hashes`` edges. Strip the lineage graph and the confinement has
nothing to project from -- the trust class stops propagating and the verdict
collapses. Mutating a provenance record or reparenting an edge therefore fails
the same signature/HMAC/anchoring checks the lineage gate already enforces
(see :func:`verify_taint`).

Trust classes, from most to least trusted:

    operator > workspace > first_party > third_party > public

``third_party`` and ``public`` are the outsider-writable classes (a hostile
web page, a crafted issue body, a third-party MCP result): an artefact whose
effective trust falls at or below ``third_party`` is *tainted*. Absence of any
provenance in the closure is fail-closed to ``public`` -- no record means the
lowest trust class.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, cast

import yaml

from bernstein import _BUNDLED_TEMPLATES_DIR  # type: ignore[reportPrivateUsage]
from bernstein.core.lineage.entry import LineageEntry, entry_hash

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from bernstein.core.lineage.identity import AgentCard
    from bernstein.core.lineage.signed_write import SignedLineageLog

logger = logging.getLogger(__name__)

PROVENANCE_ARTEFACT_KIND = "tool-result"
"""``artefact_kind`` used for a tool-result provenance record."""


class TrustClass(StrEnum):
    """Provenance trust class of a tool result / artefact."""

    OPERATOR = "operator"
    WORKSPACE = "workspace"
    FIRST_PARTY = "first_party"
    THIRD_PARTY = "third_party"
    PUBLIC = "public"


# Higher rank == more trusted. The ordering is total and fixed; taint
# projection takes the minimum rank over the closure.
_TRUST_RANK: dict[TrustClass, int] = {
    TrustClass.OPERATOR: 4,
    TrustClass.WORKSPACE: 3,
    TrustClass.FIRST_PARTY: 2,
    TrustClass.THIRD_PARTY: 1,
    TrustClass.PUBLIC: 0,
}

#: The lowest trust class. Used as the fail-closed default when provenance is
#: missing (no record means lowest trust).
LOWEST_TRUST_CLASS: TrustClass = TrustClass.PUBLIC

#: An artefact whose effective trust is at or below this class is *tainted*
#: (its bytes could have been written by an outsider).
UNTRUSTED_THRESHOLD: TrustClass = TrustClass.THIRD_PARTY


def trust_rank(tc: TrustClass) -> int:
    """Return the total-order rank of *tc* (higher == more trusted)."""
    return _TRUST_RANK[tc]


def min_trust_class(a: TrustClass, b: TrustClass) -> TrustClass:
    """Return the least-trusted of *a* and *b* (minimum rank wins)."""
    return a if _TRUST_RANK[a] <= _TRUST_RANK[b] else b


def is_untrusted(tc: TrustClass) -> bool:
    """Return True when *tc* is at or below the untrusted threshold."""
    return _TRUST_RANK[tc] <= _TRUST_RANK[UNTRUSTED_THRESHOLD]


# ---------------------------------------------------------------------------
# Deterministic taint projection
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TaintVerdict:
    """Result of projecting trust over an artefact's lineage closure.

    Every field is a pure function of the log, so two independent verifiers
    holding the same log compute a byte-identical verdict with no live
    process.

    Attributes:
        target: The entry hash whose taint was computed.
        trust: Effective trust class (minimum over the closure). ``public``
            when provenance is missing (fail closed).
        tainted: True when ``trust`` is at or below the untrusted threshold.
        resolved: False when ``target`` is not present in the log at all.
        closure: Sorted tuple of every entry hash reachable from ``target``
            (including ``target``) over ``parent_hashes`` edges.
        trust_records: Sorted tuple of ``(entry_hash, trust_class)`` for every
            provenance record found in the closure -- the signed labels the
            verdict projected from.
    """

    target: str
    trust: TrustClass
    tainted: bool
    resolved: bool
    closure: tuple[str, ...]
    trust_records: tuple[tuple[str, str], ...]


def _index_by_hash(entries: Sequence[LineageEntry]) -> dict[str, LineageEntry]:
    return {entry_hash(e): e for e in entries}


def effective_trust(target: str, entries: Sequence[LineageEntry]) -> TaintVerdict:
    """Project the minimum trust class over *target*'s lineage closure.

    Args:
        target: Entry hash (``sha256:...``) to evaluate.
        entries: Every lineage entry available (order-independent).

    Returns:
        A :class:`TaintVerdict`. When ``target`` is absent from ``entries``
        the verdict is fail-closed (``public``, tainted, ``resolved=False``).
    """
    index = _index_by_hash(entries)
    if target not in index:
        return TaintVerdict(
            target=target,
            trust=LOWEST_TRUST_CLASS,
            tainted=is_untrusted(LOWEST_TRUST_CLASS),
            resolved=False,
            closure=(),
            trust_records=(),
        )

    # Breadth-first walk of parent_hashes. A cycle is impossible in a valid
    # content-addressed log (a parent's hash is fixed before a child can name
    # it), but ``seen`` guards against a malformed input regardless.
    seen: set[str] = set()
    frontier: list[str] = [target]
    trust_records: list[tuple[str, str]] = []
    while frontier:
        h = frontier.pop()
        if h in seen:
            continue
        seen.add(h)
        entry = index.get(h)
        if entry is None:
            # A dangling parent is caught by the gate; here we simply stop
            # walking that branch (its contribution cannot be attested).
            continue
        if entry.trust_class is not None:
            trust_records.append((h, entry.trust_class))
        frontier.extend(entry.parent_hashes)

    if trust_records:
        effective = TrustClass.OPERATOR
        for _, tc in trust_records:
            effective = min_trust_class(effective, TrustClass(tc))
    else:
        # No provenance anywhere in the closure -> fail closed to lowest.
        effective = LOWEST_TRUST_CLASS

    return TaintVerdict(
        target=target,
        trust=effective,
        tainted=is_untrusted(effective),
        resolved=True,
        closure=tuple(sorted(seen)),
        trust_records=tuple(sorted(trust_records)),
    )


def resolve_artefact_tip(artefact_path: str, entries: Sequence[LineageEntry]) -> str | None:
    """Return the current-tip entry hash for *artefact_path*, or None.

    The tip is the most recent write (highest ``ts_ns``, ties broken by entry
    hash for determinism). Absent artefacts return ``None``.
    """
    candidates = [(e.ts_ns, entry_hash(e)) for e in entries if e.artefact_path == artefact_path]
    if not candidates:
        return None
    # Tuples compare by ts_ns then entry_hash, so plain max picks the latest
    # write with a deterministic tie-break.
    return max(candidates)[1]


def taint_for_artefact(artefact_path: str, entries: Sequence[LineageEntry]) -> TaintVerdict:
    """Compute the taint verdict for the current tip of *artefact_path*.

    An unknown path is fail-closed (``public``, tainted, ``resolved=False``).
    """
    tip = resolve_artefact_tip(artefact_path, entries)
    if tip is None:
        return TaintVerdict(
            target=artefact_path,
            trust=LOWEST_TRUST_CLASS,
            tainted=is_untrusted(LOWEST_TRUST_CLASS),
            resolved=False,
            closure=(),
            trust_records=(),
        )
    return effective_trust(tip, entries)


# ---------------------------------------------------------------------------
# Offline verification (fail closed on a failing gate)
# ---------------------------------------------------------------------------


class TaintVerificationError(RuntimeError):
    """Raised when a taint verdict is requested from a log that fails the gate.

    Carries the gate failures so an operator sees exactly which record broke
    (a mutated trust class, a reparented edge, a stripped signature).
    """

    def __init__(self, failures: Sequence[str]) -> None:
        self.failures: list[str] = list(failures)
        super().__init__("lineage gate failed: " + "; ".join(self.failures[:5]))


def load_entries_from_log(log_path: Path) -> list[LineageEntry]:
    """Load lineage entries from ``log.jsonl``.

    Intended to run *after* the lineage gate has verified the log, so the raw
    bytes are already known to be byte-canonical and signature-anchored. Lines
    that fail to parse are skipped (the gate is the authority on integrity).
    """
    import json

    entries: list[LineageEntry] = []
    if not log_path.exists():
        return entries
    for raw in log_path.read_bytes().split(b"\n"):
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw)
            entries.append(LineageEntry(**obj))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    return entries


def verify_taint(
    log_path: Path,
    agent_cards_dir: Path,
    target: str,
    *,
    operator_secret: bytes | None = None,
) -> TaintVerdict:
    """Run the lineage gate, then project the taint verdict for *target*.

    The gate is the tamper-evidence layer: a mutated provenance record or a
    reparented edge breaks a signature / HMAC / anchoring check, so this
    function refuses to emit a verdict from a log that does not pass. That is
    the same failure surface a tampered ordinary lineage entry hits.

    Args:
        log_path: Path to ``log.jsonl``.
        agent_cards_dir: Directory of ``<agent-id>/card.json`` cards.
        target: Entry hash (``sha256:...``) or a repo-relative artefact path.
        operator_secret: Optional operator HMAC secret; when given the gate
            also verifies each entry's ``operator_hmac``.

    Returns:
        The :class:`TaintVerdict`.

    Raises:
        TaintVerificationError: When the lineage gate reports any failure.
    """
    from bernstein.core.lineage.gate import check

    result = check(log_path=log_path, agent_cards_dir=agent_cards_dir, operator_secret=operator_secret)
    if not result.ok:
        raise TaintVerificationError(result.failures)

    entries = load_entries_from_log(log_path)
    if target.startswith("sha256:"):
        return effective_trust(target, entries)
    return taint_for_artefact(target, entries)


# ---------------------------------------------------------------------------
# Source-to-trust-class map (reviewed data)
# ---------------------------------------------------------------------------

_TRUST_SOURCES_RELPATH = ("provenance", "trust_sources.yaml")


def _coerce_trust_class(raw: object) -> TrustClass | None:
    try:
        return TrustClass(str(raw).strip().lower())
    except ValueError:
        logger.warning("Unknown trust_class token %r in trust source map - ignoring", raw)
        return None


def load_trust_source_map(*, workdir: Path | None = None) -> dict[str, TrustClass]:
    """Load the source-to-trust-class map (reviewed data).

    Resolution mirrors the capability matrix: ``<workdir>/templates/
    capabilities/trust_sources.yaml`` when present, else the bundled default.

    Returns:
        Map of source name -> :class:`TrustClass`. Malformed rows are dropped.
    """
    path: Path | None = None
    if workdir is not None:
        local = workdir / "templates" / _TRUST_SOURCES_RELPATH[0] / _TRUST_SOURCES_RELPATH[1]
        if local.is_file():
            path = local
    if path is None:
        bundled = _BUNDLED_TEMPLATES_DIR / _TRUST_SOURCES_RELPATH[0] / _TRUST_SOURCES_RELPATH[1]
        if bundled.is_file():
            path = bundled
    if path is None:
        return {}

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("Failed to load trust source map %s: %s", path, exc)
        return {}
    if not isinstance(raw, dict):
        return {}

    rows = cast("dict[str, object]", raw).get("sources", [])
    if not isinstance(rows, list):
        return {}

    out: dict[str, TrustClass] = {}
    for row in cast("list[object]", rows):
        if not isinstance(row, dict):
            continue
        entry = cast("dict[str, object]", row)
        name = entry.get("name")
        tc = _coerce_trust_class(entry.get("trust_class"))
        if isinstance(name, str) and name.strip() and tc is not None:
            out[name.strip()] = tc
    return out


def trust_class_for_source(
    source: str,
    mapping: Mapping[str, TrustClass] | None = None,
) -> TrustClass:
    """Return the trust class for *source*, fail-closed to lowest when unknown.

    Args:
        source: Source/tool name (e.g. ``web.fetch``, ``github.fetch_issue``).
        mapping: Optional pre-loaded map; the bundled default is loaded when
            ``None``.
    """
    table = mapping if mapping is not None else load_trust_source_map()
    return table.get(source, LOWEST_TRUST_CLASS)


# ---------------------------------------------------------------------------
# Recorder wiring (killer shape: the label IS a lineage record)
# ---------------------------------------------------------------------------

_SANITISE_TOOL_RE = re.compile(r"[^a-z0-9._-]+")


def _provenance_artefact_path(tool_name: str, content_hash: str) -> str:
    """Return a stable repo-relative path for a tool-result provenance record.

    The record's identity is its content hash; the path is a namespace so the
    lineage store can shard it. The tool name is sanitised to slug characters
    so it can never smuggle a traversal segment past the recorder.
    """
    slug = _SANITISE_TOOL_RE.sub("-", tool_name.strip().lower()).strip("-") or "tool"
    digest = content_hash.split(":", 1)[-1]
    return f"provenance/{slug}/{digest[:16]}"


def record_tool_result(
    recorder: SignedLineageLog,
    *,
    tool_name: str,
    result_bytes: bytes,
    trust_class: TrustClass,
    agent_id: str,
    agent_card: AgentCard,
    private_key_pem: str,
    tool_call_id: str,
    span_id: str,
    parent_hashes: list[str] | None = None,
) -> str:
    """Record a tool result as a signed, content-addressed provenance entry.

    Args:
        recorder: The signed lineage log to append through.
        tool_name: Producing surface (e.g. ``web.fetch``); only namespaces the
            path -- the trust class is authoritative.
        result_bytes: The exact tool-result bytes (content-addressed).
        trust_class: The trust class assigned to this source.
        agent_id, agent_card, private_key_pem: Signing identity.
        tool_call_id: Cross-link to the originating audit entry.
        span_id: OTel span hex.
        parent_hashes: Optional lineage edges back to upstream sources (e.g. a
            quarantine extraction pointing at the tainted payload it came from).

    Returns:
        The entry hash of the recorded provenance entry.
    """
    content_hash = "sha256:" + hashlib.sha256(result_bytes).hexdigest()
    artefact_path = _provenance_artefact_path(tool_name, content_hash)
    return recorder.record_write(
        artefact_path=artefact_path,
        new_content=result_bytes,
        agent_id=agent_id,
        agent_card=agent_card,
        private_key_pem=private_key_pem,
        tool_call_id=tool_call_id,
        span_id=span_id,
        artefact_kind=PROVENANCE_ARTEFACT_KIND,
        trust_class=str(trust_class),
        extra_parents=parent_hashes,
    )


__all__ = [
    "LOWEST_TRUST_CLASS",
    "PROVENANCE_ARTEFACT_KIND",
    "UNTRUSTED_THRESHOLD",
    "TaintVerdict",
    "TaintVerificationError",
    "TrustClass",
    "effective_trust",
    "is_untrusted",
    "load_entries_from_log",
    "load_trust_source_map",
    "min_trust_class",
    "record_tool_result",
    "resolve_artefact_tip",
    "taint_for_artefact",
    "trust_class_for_source",
    "trust_rank",
    "verify_taint",
]
