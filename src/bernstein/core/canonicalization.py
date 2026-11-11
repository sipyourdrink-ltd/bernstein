"""Shared legacy JSON bytes for evidence, governance and review signatures.

This format preserves Python JSON's number spellings and code-point key order;
it is not RFC 8785 (JCS). Changing it requires a versioned payload migration.
"""

from __future__ import annotations

import json
from typing import Any


def canonical_bytes(payload: dict[str, Any]) -> bytes:
    """Return sorted, compact UTF-8 JSON, preserving legacy non-finite floats."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
