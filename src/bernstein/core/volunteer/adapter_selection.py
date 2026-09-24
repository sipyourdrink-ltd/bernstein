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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path
from dataclasses import dataclass

from bernstein.core.endpoints import certified_roles_for_endpoint

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EndpointRef:
    """A resolved local endpoint candidate."""

    base_url: str
    model: str


def select_adapter_for_volunteer(
    role: str | None,
    explicit_adapter: str | None,
    workdir: Path,
    local_endpoint: EndpointRef | None = None,
) -> str | None:
    """Select adapter for volunteer mode with local-first default posture.

    Args:
        role: The task role (e.g. "backend", "qa").
        explicit_adapter: Adapter explicitly chosen by the donor, or None.
        workdir: Working directory to check for endpoint certification.
        local_endpoint: A resolved local endpoint candidate, or None if none
            is configured by the donor.

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

    if not local_endpoint:
        _logger.warning(
            "No adapter chosen and no local endpoint candidate supplied. Falling back to configured default."
        )
        return None

    # Check if a local endpoint is certified for this role
    try:
        certified = certified_roles_for_endpoint(workdir, local_endpoint.base_url, local_endpoint.model)
        if role in certified:
            _logger.info("No adapter chosen; selecting local endpoint (certified for role=%s)", role)
            return "local"
    except TypeError:
        raise
    except Exception as e:
        _logger.warning("Failed to check certified local endpoints: %s", e)

    # No explicit choice, and local endpoint isn't certified
    _logger.warning(
        "No adapter chosen and no certified local endpoint for role=%s. "
        "Falling back to configured default. Consider certifying a local endpoint "
        "to minimize provider observability.",
        role,
    )
    return None
