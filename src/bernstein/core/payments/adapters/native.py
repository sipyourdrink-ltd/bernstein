"""Bernstein-native mandate adapter.

The external representation is simply the mandate's JCS-canonical wire dict as
UTF-8 bytes -- the same bytes the content hash addresses. Round-tripping is
trivially byte-identical because there is no reshaping.
"""

from __future__ import annotations

import json

from bernstein.core.payments.mandate import SpendMandate
from bernstein.core.security.agent_card_signer import canonicalize_jcs

__all__ = ["NativeMandateAdapter"]


class NativeMandateAdapter:
    """Projects a mandate to/from its native canonical JSON bytes."""

    name: str = "bernstein-native"

    def to_external(self, mandate: SpendMandate) -> bytes:
        """Return the mandate's JCS-canonical wire bytes."""
        return canonicalize_jcs(mandate.to_dict())

    def from_external(self, blob: bytes) -> SpendMandate:
        """Parse native canonical JSON bytes back into a mandate."""
        return SpendMandate.from_dict(json.loads(blob))
