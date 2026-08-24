"""Per-part content-hash receipt for the agent context prompt.

A :class:`ContextReceipt` records, for each named section actually included
in an agent's context prompt, a SHA-256 content hash plus token/character
estimates.  This gives a deterministic, auditable fingerprint of what was
sent to the model so downstream tooling can detect drift or reproduce a
prompt exactly.

Usage::

    from bernstein.core.agents.context_receipt import build_context_receipt

    receipt = build_context_receipt([("role", role_text), ("tasks", tasks_text)])
    print(receipt.total_token_estimate)
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from typing import Any

from bernstein.core.tokens.token_estimation import estimate_tokens_for_text


@dataclass
class ContextReceiptEntry:
    """Content-hash receipt for a single named context section.

    Attributes:
        label: Section name, e.g. ``"role"``, ``"tasks"``, ``"lessons"``.
        content_sha256: SHA-256 hex digest of the section content (UTF-8 bytes).
        token_estimate: Estimated token count via :func:`estimate_tokens_for_text`.
        char_count: Raw character count of the section content.
    """

    label: str
    content_sha256: str
    token_estimate: int
    char_count: int

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ContextReceiptEntry:
        """Reconstruct an entry from a dict produced by :meth:`to_dict`."""
        return cls(
            label=d["label"],
            content_sha256=d["content_sha256"],
            token_estimate=d["token_estimate"],
            char_count=d["char_count"],
        )


@dataclass
class ContextReceipt:
    """Ordered receipt of all context sections actually included.

    Attributes:
        entries: One :class:`ContextReceiptEntry` per included section, in order.
        total_token_estimate: Sum of all ``entry.token_estimate``.
        total_chars: Sum of all ``entry.char_count``.
        section_count: Number of entries.
    """

    entries: list[ContextReceiptEntry]
    total_token_estimate: int
    total_chars: int
    section_count: int

    @property
    def total_tokens(self) -> int:
        """Convenience alias for :attr:`total_token_estimate`."""
        return self.total_token_estimate

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ContextReceipt:
        """Reconstruct a receipt from a dict produced by :meth:`to_dict`."""
        entries = [ContextReceiptEntry.from_dict(e) for e in d["entries"]]
        return cls(
            entries=entries,
            total_token_estimate=d["total_token_estimate"],
            total_chars=d["total_chars"],
            section_count=d["section_count"],
        )


def build_context_receipt(named_sections: list[tuple[str, str]]) -> ContextReceipt:
    """Build a receipt from ``(label, content)`` pairs.

    For each pair, computes the SHA-256 content hash, token estimate, and
    character count, then sums the token and character totals across all
    entries.

    Args:
        named_sections: Ordered ``(label, content)`` pairs, one per section
            actually included in the context prompt.

    Returns:
        A :class:`ContextReceipt` with one entry per section.
    """
    entries: list[ContextReceiptEntry] = []
    for label, content in named_sections:
        entries.append(
            ContextReceiptEntry(
                label=label,
                content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                token_estimate=estimate_tokens_for_text(content, assumed_type="text"),
                char_count=len(content),
            )
        )
    return ContextReceipt(
        entries=entries,
        total_token_estimate=sum(e.token_estimate for e in entries),
        total_chars=sum(e.char_count for e in entries),
        section_count=len(entries),
    )
