"""Modules that register a receipt kind with the shared protocol.

:func:`bernstein.core.receipts.protocol.verify_receipt` imports these before it
looks a kind up, so a holder of a receipt document does not have to import the
producing module first. A module that registers a kind belongs here; leaving it
out means its receipts verify only when something else has already imported it.
"""

from __future__ import annotations

__all__ = ["RECEIPT_KIND_MODULES"]

#: Dotted module paths, imported once by the protocol's kind loader.
RECEIPT_KIND_MODULES: tuple[str, ...] = (
    "bernstein.core.planning.recovery_receipt",
    "bernstein.core.security.change_receipt",
)
