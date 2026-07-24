"""Protocol adapters for external signed-mandate interop.

The core ships two scheme-agnostic adapters conforming to :class:`MandateAdapter`:

* :class:`NativeMandateAdapter` -- the mandate's own canonical bytes.
* :class:`JwsPassthroughMandateAdapter` -- a generic JWS General JSON envelope.

Concrete third-party schemes plug in as out-of-tree adapters; the core blesses
none of them.
"""

from __future__ import annotations

from bernstein.core.payments.adapters.base import MandateAdapter
from bernstein.core.payments.adapters.jws_passthrough import JwsPassthroughMandateAdapter
from bernstein.core.payments.adapters.native import NativeMandateAdapter

__all__ = [
    "JwsPassthroughMandateAdapter",
    "MandateAdapter",
    "NativeMandateAdapter",
]
