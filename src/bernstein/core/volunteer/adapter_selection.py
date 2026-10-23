"""Adapter selection for volunteer mode — local-first default posture.

When a donor runs volunteer tasks, the default adapter choice should prefer
local/self-hosted execution when available.  This module implements the
selection logic: if no adapter was explicitly chosen and a certified local
endpoint exists for the needed role, select the registered local adapter
(:data:`LOCAL_ADAPTER_ID`); otherwise, fall back to whatever the donor
configured (with a warning logged).

What the selection does and does not do: it names the adapter id the
auth-basis gate evaluates.  The process that is actually launched is still the
one the caller's ``agent_argv`` builder returns, so the selected id is not a
guarantee about which model a run talks to.

The selection function MUST NOT read provider credentials (e.g. ANTHROPIC_API_KEY)
to decide — the decision is based purely on explicit policy (was an adapter
chosen?) and certified capabilities (is a local endpoint available?).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bernstein.core.endpoints import certified_roles_for_endpoint, discover_default_model, normalize_base_url
from bernstein.core.security.deployment_profile import is_local_or_eu_host

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

_logger = logging.getLogger(__name__)

#: Registry name of the adapter that drives a local OpenAI-compatible endpoint
#: (Ollama, vLLM, llama.cpp, LM Studio).  It is a registered adapter whose
#: contract pins ``auth.basis: local``, so the auth-basis gate accepts it.
LOCAL_ADAPTER_ID = "ollama"

#: Seconds the endpoint probe may take.  Short on purpose: it runs before the
#: task is claimed and is not part of the donor's wall-clock loan.
PROBE_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class EndpointRef:
    """A resolved local endpoint candidate."""

    base_url: str
    model: str


def discover_local_endpoint(environ: Mapping[str, str]) -> EndpointRef | None:
    """Resolve a local endpoint candidate from ``OPENAI_BASE_URL``.

    Never raises.  The ``/models`` probe is attempted only for a host that is
    local (loopback, private range, ``*.internal``/``*.local``/``*.svc``): the
    donor's ``OPENAI_API_KEY`` is sent to that probe, so a hosted or public
    host never receives it, over any scheme.  A host that is not local yields
    no candidate rather than a probe.

    Args:
        environ: Environment mapping to read ``OPENAI_BASE_URL`` and
            ``OPENAI_API_KEY`` from.

    Returns:
        The candidate endpoint, or ``None`` when none is configured, the host
        is not local, or the endpoint does not list a model.
    """
    raw_base_url = environ.get("OPENAI_BASE_URL", "").strip()
    if not raw_base_url:
        return None
    base_url = normalize_base_url(raw_base_url)
    if not is_local_or_eu_host(base_url):
        _logger.warning("OPENAI_BASE_URL does not name a local endpoint; not probing it or sending credentials to it.")
        return None
    model = discover_default_model(
        base_url=base_url,
        api_key=environ.get("OPENAI_API_KEY") or None,
        timeout=PROBE_TIMEOUT_SECONDS,
    )
    return EndpointRef(base_url=base_url, model=model) if model else None


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
        workdir: The donor's project root -- the directory
            ``bernstein doctor --endpoint`` was run from, which holds the
            ``.sdd/endpoints/certifications`` receipts.  Not the per-task
            scratch workspace.
        local_endpoint: A resolved local endpoint candidate, or None if none
            is configured by the donor.

    Returns:
        Adapter ID to use: the explicit choice if given,
        :data:`LOCAL_ADAPTER_ID` if a certified local endpoint exists for the
        role, or None if neither applies (caller
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

    # "Certified" means the endpoint passed capability probes, not that it is
    # self-hosted, so locality is checked separately.
    if not is_local_or_eu_host(local_endpoint.base_url):
        _logger.warning("Endpoint candidate is not a local host; not selecting a local adapter.")
        return None

    # Check if a local endpoint is certified for this role
    try:
        certified = certified_roles_for_endpoint(workdir, local_endpoint.base_url, local_endpoint.model)
        if role in certified:
            _logger.info("No adapter chosen; selecting local endpoint (certified for role=%s)", role)
            return LOCAL_ADAPTER_ID
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
