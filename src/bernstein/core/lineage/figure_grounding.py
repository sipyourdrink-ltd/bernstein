"""Resolve report-figure anchors against the signed lineage log (issue #2888).

The pure ``figures_grounded`` evaluator in :mod:`bernstein.core.tasks.figures`
takes an *injected* anchor resolver so the tokenizer and sidecar can be tested
without any lineage state. This module supplies the real resolver: it reads the
signed, HMAC-chained lineage log and, for each anchor, finds the lineage record
whose ``content_hash`` (attachment / artifact) matches the anchor's ref and
verifies that record end to end - Ed25519 signature (kid-bound), operator HMAC
(when a secret is available), and chain anchoring (its parents are present).

A figure is only as good as its receipt: a tampered or missing target record
makes the anchor fail, so a fabricated figure whose anchor points at nothing -
or at an altered record - can never pass.

The resolver dispatches by anchor kind through a small registry. The
``receipt`` kind (issue #2887, the query-receipt subsystem) is a declared plug
point: registering its resolver is a one-line addition once receipts exist, and
nothing else here changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from bernstein.core.lineage.entry import canonicalise, compute_operator_hmac, entry_hash
from bernstein.core.lineage.gate import _load_cards
from bernstein.core.lineage.identity import jws_header_kid, verify_detached
from bernstein.core.tasks.figures import (
    AnchorResolution,
    FigureAnchor,
    FiguresVerdict,
    TokenizerPolicy,
    evaluate_figures_grounded_bytes,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from bernstein.core.lineage.entry import LineageEntry
    from bernstein.core.lineage.identity import AgentCard


def _short(content_hash: str) -> str:
    stem = content_hash.split(":", 1)[1] if ":" in content_hash else content_hash
    return "sha256:" + stem[:12]


@dataclass
class LineageAnchorResolver:
    """Resolve figure anchors against a signed lineage log on disk.

    The log is read once at construction; each anchor resolution is then a
    dictionary lookup by ``content_hash`` plus a per-record cryptographic
    verification, so a report with many figures pays one log read.
    """

    log_path: Path
    cards_dir: Path
    operator_secret: bytes | None = None

    _by_hash: dict[str, list[tuple[LineageEntry, str, int]]] = field(init=False, default_factory=dict)
    _hashes: set[str] = field(init=False, default_factory=set)
    _cards: dict[tuple[str, str], AgentCard] = field(init=False, default_factory=dict)
    _resolvers: dict[str, Callable[[str], AnchorResolution]] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        from bernstein.core.lineage.store import LineageStore

        store = LineageStore(self.log_path.parent)
        for idx, (entry, jws) in enumerate(store.read_log(), start=1):
            self._by_hash.setdefault(entry.content_hash, []).append((entry, jws, idx))
            self._hashes.add(entry_hash(entry))
        self._cards, _ = _load_cards(self.cards_dir)
        # Anchor-kind registry. attachment and artifact both address a lineage
        # record by content hash; receipt is the reserved plug point (#2887).
        self._resolvers = {
            "attachment": lambda ref: self._resolve_by_content_hash("attachment", ref),
            "artifact": lambda ref: self._resolve_by_content_hash("artifact", ref),
            "receipt": self._resolve_receipt,
        }

    def register(self, kind: str, resolver: Callable[[str], AnchorResolution]) -> None:
        """Register (or override) a resolver for an anchor kind.

        The receipt-id resolver (issue #2887) plugs in here without touching the
        evaluator or the verify path.
        """
        self._resolvers[kind] = resolver

    def resolve(self, anchor: FigureAnchor) -> AnchorResolution:
        resolver = self._resolvers.get(anchor.kind)
        if resolver is None:
            return AnchorResolution(ok=False, statement=f"no resolver for anchor kind {anchor.kind!r}")
        return resolver(anchor.ref)

    # -- per-kind resolvers --------------------------------------------------

    def _resolve_by_content_hash(self, kind: str, ref: str) -> AnchorResolution:
        candidates = self._by_hash.get(ref)
        if not candidates:
            return AnchorResolution(ok=False, statement=f"anchor {ref} resolves to no lineage record")
        # A content hash can appear on more than one record; the anchor is
        # grounded if any matching record verifies.
        reasons: list[str] = []
        for entry, jws, idx in candidates:
            ok, reason = self._verify_record(entry, jws)
            if ok:
                return AnchorResolution(
                    ok=True,
                    statement=f"traces to {kind} {_short(ref)}, recorded at chain position {idx}",
                )
            reasons.append(reason)
        return AnchorResolution(ok=False, statement=f"anchor {ref} record does not verify: {reasons[0]}")

    def _resolve_receipt(self, ref: str) -> AnchorResolution:
        # Plug point for the query-receipt subsystem (issue #2887). Until it
        # lands, a receipt anchor cannot be resolved; fail closed with a clear
        # reason rather than silently accepting an ungrounded figure.
        return AnchorResolution(
            ok=False,
            statement=f"receipt anchor {ref!r} requires the query-receipt subsystem (issue #2887), not available",
        )

    # -- record verification -------------------------------------------------

    def _verify_record(self, entry: LineageEntry, jws: str) -> tuple[bool, str]:
        """Verify one record's signature, HMAC, and chain anchoring."""
        eh = entry_hash(entry)
        card = self._cards.get((entry.agent_id, entry.agent_card_kid))
        if card is None:
            return False, f"no agent card for (agent_id={entry.agent_id!r}, kid={entry.agent_card_kid!r})"
        if not jws:
            return False, f"missing signature sidecar for entry {eh}"
        if jws_header_kid(jws) != entry.agent_card_kid:
            return False, f"kid binding mismatch on entry {eh}"
        if not verify_detached(canonicalise(entry), jws, card):
            return False, f"invalid signature on entry {eh}"
        if self.operator_secret is not None:
            import hmac as _hmac

            expected = compute_operator_hmac(entry, self.operator_secret)
            if not _hmac.compare_digest(expected, entry.operator_hmac):
                return False, f"HMAC mismatch on entry {eh}"
        for ph in entry.parent_hashes:
            if ph not in self._hashes:
                return False, f"dangling parent_hash {ph} on entry {eh}"
        return True, ""


def verify_report_figures(
    *,
    canonical_bytes: bytes,
    log_path: Path,
    cards_dir: Path,
    operator_secret: bytes | None,
    policy: TokenizerPolicy | None = None,
) -> FiguresVerdict:
    """Run ``figures_grounded`` on report-bundle bytes against the lineage log.

    ``canonical_bytes`` are the stored canonical artifact bytes (a report
    bundle). Raises :class:`bernstein.core.tasks.artifacts.CanonicalisationError`
    when the bytes are not a report bundle - callers that may see a plain report
    should guard with :func:`bernstein.core.tasks.figures.is_report_bundle`.
    """
    resolver = LineageAnchorResolver(log_path=log_path, cards_dir=cards_dir, operator_secret=operator_secret)
    return evaluate_figures_grounded_bytes(canonical_bytes, resolve_anchor=resolver.resolve, policy=policy)


__all__ = ["LineageAnchorResolver", "verify_report_figures"]
