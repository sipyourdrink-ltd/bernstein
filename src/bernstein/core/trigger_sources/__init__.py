"""Trigger source adapters — normalize raw events into TriggerEvent."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from bernstein.core.models import TriggerEvent


class TriggerSource(Protocol):
    """Protocol for trigger source adapters."""

    def normalize(self, raw_event: dict[str, Any]) -> TriggerEvent:
        """Convert a raw event into a normalized TriggerEvent."""
        ...
