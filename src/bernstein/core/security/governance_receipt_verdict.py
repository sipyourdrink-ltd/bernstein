"""What a dropped receipt actually proves, said in the verdict itself (#5067).

:func:`~bernstein.core.replay.run_receipt.verify_run_receipt` already draws the
distinction that matters here: a pass against the key *embedded in the receipt*
is trust-on-first-use and proves the file is internally consistent, while a
pass against a key the operator pinned out of band additionally proves who
produced it. The CLI says which tier it reached; nothing projected that
distinction into a document, so any second surface wrapping the verifier would
have had a boolean to render and would have rendered a tick.

This module is that projection. It answers one question about one uploaded
file, and it answers it with the tier attached:

``tier``
    ``"integrity-only"`` on a pass, ``None`` when the receipt did not verify at
    all. There is no third value: this projection is for a receipt whose only
    trust anchor is itself, so the provenance tier is not reachable through it
    and is not offered as a field a caller could see set.

``caveat``
    Present exactly when ``tier`` is set, naming the key source in prose the
    screen renders beside the tick. A failure claims nothing, so it qualifies
    nothing and carries no caveat.

Everything else is the verifier's own result carried through unchanged --
``status``, the walked ranges, the first divergent journal step, the errors --
so a verdict read off a screen and one recomputed offline with
``bernstein verify receipt <file> --json`` are statements about the same bytes.

Determinism: the document is canonical JSON (sorted keys, minimal separators)
and a pure function of the receipt bytes. Nothing under ``.sdd`` is read and no
key material is needed, which is the same guarantee the offline verifier makes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from bernstein.core.replay.run_receipt import verify_run_receipt

#: Version stamped into the verdict document. Bump only on a shape change.
VERDICT_DOCUMENT_VERSION = 1

#: The only tier a self-anchored receipt can reach. Matches the label
#: ``bernstein verify receipt --json`` reports for the same file.
TIER_INTEGRITY_ONLY = "integrity-only"

#: Why the tick is not a provenance claim. Rendered beside the result, not
#: behind a disclosure: a console that shows "verified" while meaning
#: "self-consistent" is the thing this screen exists not to be.
INTEGRITY_ONLY_CAVEAT = (
    "The signature was checked against the key embedded in the receipt "
    "(trust-on-first-use). That proves the file is internally consistent and "
    "that no byte changed after signing; it does not prove who produced it, "
    "because a forger holding the whole file could re-sign it with their own "
    "key. For provenance, pin the operator's key out of band: "
    "bernstein verify receipt <file> --public-key <pem>."
)


@dataclass(frozen=True, slots=True)
class ReceiptVerdict:
    """The verifier's outcome for one uploaded receipt, with its tier."""

    ok: bool
    status: str
    tier: str | None
    caveat: str | None
    run_id: str
    journal_events: int
    spine_entries: int
    divergent_step: int | None
    errors: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": VERDICT_DOCUMENT_VERSION,
            "ok": self.ok,
            "status": self.status,
            "tier": self.tier,
            "caveat": self.caveat,
            "run_id": self.run_id,
            "journal_events": self.journal_events,
            "spine_entries": self.spine_entries,
            "divergent_step": self.divergent_step,
            "errors": list(self.errors),
        }


def collect_receipt_verdict(receipt_bytes: bytes) -> ReceiptVerdict:
    """Verify *receipt_bytes* offline and label what the pass proves.

    Args:
        receipt_bytes: The uploaded file, verbatim. Anything that is not a
            well-formed receipt -- an empty upload included -- is a
            ``"malformed"`` verdict about that file rather than an error:
            "this file attests nothing" is an answer the operator came for.

    Returns:
        The :class:`ReceiptVerdict`. ``tier`` and ``caveat`` are set together
        and only on a pass.
    """
    result = verify_run_receipt(receipt_bytes)
    tier = TIER_INTEGRITY_ONLY if result.ok else None
    return ReceiptVerdict(
        ok=result.ok,
        status=result.status,
        tier=tier,
        caveat=INTEGRITY_ONLY_CAVEAT if tier is not None else None,
        run_id=result.run_id,
        journal_events=result.journal_events,
        spine_entries=result.spine_entries,
        divergent_step=result.divergent_step,
        errors=list(result.errors),
    )


def receipt_verdict_json(receipt_bytes: bytes) -> str:
    """Return the canonical verdict document for *receipt_bytes*.

    **The** entry point: the drop-verify route returns exactly these bytes, so
    the screen and an offline re-verification cannot differ about one file.
    """
    payload = collect_receipt_verdict(receipt_bytes).to_dict()
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


__all__ = [
    "INTEGRITY_ONLY_CAVEAT",
    "TIER_INTEGRITY_ONLY",
    "VERDICT_DOCUMENT_VERSION",
    "ReceiptVerdict",
    "collect_receipt_verdict",
    "receipt_verdict_json",
]
