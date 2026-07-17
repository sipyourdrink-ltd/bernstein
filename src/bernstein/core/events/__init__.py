"""Eventing v2 - the chain-projected unified event feed (#2548).

This package defines one canonical event grammar as a deterministic projection
over the HMAC-chained audit log, and builds the composable trigger language and
the receipt-backed action vocabulary on that same projection. The thing an
operator queries, alerts on, and acts from is the chain itself:

* :mod:`grammar` - the canonical event and the pure projection function;
* :mod:`feed` - the fence-posted, offline-verifiable window;
* :mod:`triggers` - threshold, absence, sequence, and compound semantics as
  deterministic folds over a window;
* :mod:`actions` - the typed action vocabulary and the fire-receipt gate;
* :mod:`receipts` - fire receipts and absence (negative-proof) receipts;
* :mod:`webhook_template` - declarative payload-to-event rendering;
* :mod:`state` - per-project, process-isolated counter and expectation state.

Strip the chain and this is an event bus with a log: every feed event is a
projected chain entry keyed by its HMAC, and every automation action is a signed
receipt bound to the triggering event's hash.
"""

from __future__ import annotations

from bernstein.core.events.grammar import (
    CanonicalEvent,
    canonical_details_digest,
    project_entry,
)

__all__ = [
    "CanonicalEvent",
    "canonical_details_digest",
    "project_entry",
]
