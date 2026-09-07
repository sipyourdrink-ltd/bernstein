"""Harness fingerprint computation for bench submission bundles.

The harness fingerprint is a deterministic SHA-256 over a canonical JSON
representation of the run-shaping settings. Two bundles with differing
fingerprints represent different harness configurations (prompts, tools,
budgets, sandboxes) and must not be compared directly as if the underlying
model changed.

Canonical keys included in the fingerprint:
- ``decomposition``: Dict configuring task decomposition depth and fanout.
- ``effort``: Thinking/effort tier or budget.
- ``prompt_templates``: Mapping of template name to template content hash.
- ``retry_policy``: Dict of retry thresholds and backoff strategies.
- ``sandbox``: Sandbox isolation type or profile.
- ``timeouts``: Dict of per-task and per-step timeout limits.
- ``tool_allowlist``: Sorted list of permitted tools.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

CANONICAL_KEYS = (
    "decomposition",
    "effort",
    "prompt_templates",
    "retry_policy",
    "sandbox",
    "timeouts",
    "tool_allowlist",
)


def compute_harness_fingerprint(settings: dict[str, Any] | None) -> str:
    """Compute a SHA-256 fingerprint over canonical harness settings.

    Keys included:
      - ``decomposition``: decomposition configuration or None
      - ``effort``: reasoning / thinking effort setting
      - ``prompt_templates``: mapping of prompt template names to content hashes
      - ``retry_policy``: retry parameters
      - ``sandbox``: execution sandbox profile
      - ``timeouts``: task / step timeout bounds
      - ``tool_allowlist``: allowed tool names (sorted)

    Settings outside CANONICAL_KEYS or ephemeral values (timestamps, paths)
    are ignored.
    """
    raw = settings or {}
    canonical: dict[str, Any] = {}
    for key in CANONICAL_KEYS:
        val = raw.get(key)
        if key == "tool_allowlist" and isinstance(val, (list, set, tuple)):
            canonical[key] = sorted(str(t) for t in val)
        elif key == "prompt_templates" and isinstance(val, dict):
            canonical[key] = {k: str(v) for k, v in sorted(val.items())}
        else:
            canonical[key] = val

    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def find_differing_settings(
    settings_a: dict[str, Any] | None,
    settings_b: dict[str, Any] | None,
) -> list[str]:
    """Return the list of canonical keys that differ between two settings dicts."""
    raw_a = settings_a or {}
    raw_b = settings_b or {}
    differing: list[str] = []
    for key in CANONICAL_KEYS:
        val_a = raw_a.get(key)
        val_b = raw_b.get(key)
        if key == "tool_allowlist":
            norm_a = sorted(str(t) for t in val_a) if isinstance(val_a, (list, set, tuple)) else val_a
            norm_b = sorted(str(t) for t in val_b) if isinstance(val_b, (list, set, tuple)) else val_b
            if norm_a != norm_b:
                differing.append(key)
        elif key == "prompt_templates":
            norm_a = {k: str(v) for k, v in sorted(val_a.items())} if isinstance(val_a, dict) else val_a
            norm_b = {k: str(v) for k, v in sorted(val_b.items())} if isinstance(val_b, dict) else val_b
            if norm_a != norm_b:
                differing.append(key)
        else:
            if val_a != val_b:
                differing.append(key)
    return differing
