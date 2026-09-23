"""Adapter selection for volunteer mode — local-first default posture.

When a donor runs volunteer tasks, the default adapter choice should minimize
provider observability by preferring local/self-hosted execution when available.
This module implements the selection logic: if no adapter was explicitly chosen
and a certified local endpoint exists for the needed role, use that endpoint;
otherwise, fall back to whatever the donor configured (with a warning logged).

The selection function MUST NOT read provider credentials (e.g. ANTHROPIC_API_KEY)
to decide — the decision is based purely on explicit policy (was an adapter
chosen?) and certified capabilities (is a local endpoint available?).
"""

from __future__ import annotations

import logging

from bernstein.core.endpoints import certified_roles_for_endpoint

_logger = logging.getLogger(__name__)


def select_adapter_for_volunteer(
    role: str,
    explicit_adapter: str | None,
) -> str | None:
    """Select adapter for volunteer mode with local-first default posture.

    Args:
        role: The task role (e.g. "backend", "qa").
        explicit_adapter: Adapter explicitly chosen by the donor, or None.

    Returns:
        Adapter ID to use: the explicit choice if given, "local" if a certified
        local endpoint exists for the role, or None if neither applies (caller
        should fall back with warning).

    The function never reads provider credentials to decide. The decision is
    derivable from explicit policy and certified capabilities only.
    """
    # Explicit choice overrides local-first default
    if explicit_adapter:
        return explicit_adapter

    # Check if a local endpoint is certified for this role
    try:
        certified = certified_roles_for_endpoint()
        if role in certified:
            _logger.info("No adapter chosen; selecting local endpoint (certified for role=%s)", role)
            return "local"
    except Exception as e:
        _logger.warning("Failed to check certified local endpoints: %s", e)

    # No explicit choice, no local endpoint available
    _logger.warning(
        "No adapter chosen and no certified local endpoint for role=%s. "
        "Falling back to configured default. Consider certifying a local endpoint "
        "to minimize provider observability.",
        role,
    )
    return None
