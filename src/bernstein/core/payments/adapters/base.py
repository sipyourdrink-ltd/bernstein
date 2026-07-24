"""``MandateAdapter`` - the narrow protocol for external mandate interop.

A signed :class:`~bernstein.core.payments.mandate.SpendMandate` can be projected
to an external signed-mandate representation and parsed back without the core
declaring any one external scheme canonical. Concrete third-party schemes are
implemented as out-of-tree adapters conforming to this protocol; the core ships
only a bernstein-native adapter and a generic JWS pass-through.

The round-trip contract is exact: for any adapter ``a`` and mandate ``m``,
``a.from_external(a.to_external(m)).to_dict() == m.to_dict()``. Interop must not
silently drop or reshape a signed field.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from bernstein.core.payments.mandate import SpendMandate

__all__ = ["MandateAdapter"]


@runtime_checkable
class MandateAdapter(Protocol):
    """Bidirectional projection between a ``SpendMandate`` and an external blob."""

    #: Stable, scheme-agnostic adapter name (no external product name).
    name: str

    def to_external(self, mandate: SpendMandate) -> bytes:
        """Project *mandate* to its external byte representation."""
        ...

    def from_external(self, blob: bytes) -> SpendMandate:
        """Parse an external byte *blob* back into a ``SpendMandate``."""
        ...
