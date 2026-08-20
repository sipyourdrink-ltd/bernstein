"""Runtime capability cards for the Bernstein MCP server.

The MCP ``initialize`` handshake advertises a small static ``capabilities``
object (which message families the server supports). That static manifest
does not describe the *runtime* shape an operator needs to point a client at
the server: which transports are reachable, which auth modes are configured,
which tool tier is active, whether the cost-meter envelope is on, and which
spec revision the server speaks.

A capability card answers those questions at request time. It is built from
live process state (env vars, the resolved tier, the meter toggle) rather
than a baked-in constant, so a client that fetches the card sees the server
as it is actually running. Hosts that auto-discover MCP servers can use the
card to decide how to connect without trial and error.

The card is exposed two ways, both returning the same dict:

  * over the FastMCP server as the ``bernstein://capability`` resource;
  * on the streamable HTTP transport's ``initialize`` result under the
    ``capabilityCard`` key, alongside the spec ``capabilities`` object.
"""

from __future__ import annotations

import json
import os
from importlib.metadata import PackageNotFoundError, metadata, version
from typing import TYPE_CHECKING, Any

from bernstein.core.protocols.mcp.tool_tiers import (
    resolve_active_tier,
    tier_audit,
    tools_for_tier,
)
from bernstein.mcp.cost_meter import COST_METER_ENV, cost_meter_enabled

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

#: MCP spec revision the Bernstein server targets. Kept in one place so the
#: card and the HTTP ``initialize`` response cannot drift apart.
SPEC_REVISION: str = "2025-03-26"

#: URI under which the capability card is exposed as an MCP resource.
CAPABILITY_RESOURCE_URI: str = "bernstein://capability"


def package_version() -> str:
    """Return the installed Bernstein distribution version."""
    try:
        return version("bernstein")
    except PackageNotFoundError:
        return "0+unknown"


def package_repo_url() -> str:
    """Return the repository URL declared in installed package metadata."""
    try:
        meta = metadata("bernstein")
        for project_url in meta.get_all("Project-URL") or []:
            name, _, url = project_url.partition(",")
            if name.strip().lower() in ("repository", "source"):
                return url.strip()
    except PackageNotFoundError:
        pass
    return ""


def _auth_modes() -> dict[str, Any]:
    """Describe the auth paths the running server supports.

    Reports the modes the transport layer can enforce (anonymous on
    loopback, static bearer token, OAuth-2 PKCE discovery) and whether a
    token is currently configured in the environment. When the OAuth issuer
    env var is set, the discovery surface is advertised under ``oauth`` and
    ``oauth2_pkce`` moves from ``planned`` into ``supported``.
    """
    from bernstein.mcp.oauth import capability_card_oauth, oauth_discovery_enabled

    token_configured = any(os.environ.get(name) for name in ("BERNSTEIN_MCP_TOKEN", "BERNSTEIN_MCP_AUTH_TOKEN"))
    oauth_active = oauth_discovery_enabled()
    supported = ["anonymous", "bearer"]
    planned = ["oidc"]
    if oauth_active:
        supported.append("oauth2_pkce")
    else:
        planned.insert(0, "oauth2_pkce")
    return {
        "supported": supported,
        "planned": planned,
        "bearer_token_configured": token_configured,
        "anonymous_scope": "loopback-only",
        "oauth": capability_card_oauth(),
    }


def _transports() -> list[dict[str, Any]]:
    """Describe the transports a client can use to reach the server."""
    return [
        {
            "type": "stdio",
            "default": True,
            "streaming": False,
            "command": "bernstein mcp",
        },
        {
            "type": "sse",
            "default": False,
            "streaming": True,
            "command": "bernstein mcp --transport http",
        },
        {
            "type": "http",
            "default": False,
            "streaming": True,
            "path": "/mcp",
            "cancellable": True,
        },
    ]


def build_capability_card(spec_revision: str | None = None) -> dict[str, Any]:
    """Build the runtime capability card from live process state.

    The card is intentionally self-describing and stable in shape so a client
    can consume it without a Bernstein-specific schema.

    Args:
        spec_revision: The revision actually negotiated for the transport
            serving the card. The remote transport passes the per-request
            negotiated value so the card describes the live connection
            rather than a constant (#3084); ``None`` falls back to the
            targeted :data:`SPEC_REVISION`.

    Returns:
        A JSON-serialisable dict describing transports, auth, tool tiers, the
        cost-meter state, and the negotiated spec revision.
    """
    active_tier = resolve_active_tier()
    return {
        "name": "bernstein",
        "version": package_version(),
        "repo_url": package_repo_url(),
        "specRevision": spec_revision or SPEC_REVISION,
        "transports": _transports(),
        "auth": _auth_modes(),
        "tools": {
            "activeTier": active_tier,
            "tierEnvVar": "BERNSTEIN_MCP_TOOL_TIER",
            "advertised": tools_for_tier(active_tier),
            "tiers": tier_audit(),
        },
        "prompts": {
            "supported": True,
            "catalogue": ["orchestrate_goal", "triage_failed_tasks", "cost_recap"],
        },
        "costMeter": {
            "enabled": cost_meter_enabled(),
            "envVar": COST_METER_ENV,
            "envelopeKeys": ["result", "_meter"],
        },
        "observability": {
            "perCallMeter": cost_meter_enabled(),
            "fields": ["latency_ms", "cost_usd", "call_id", "ok", "ts"],
        },
    }


def register_capability_resource(mcp: FastMCP[None]) -> None:
    """Register the runtime capability card as an MCP resource.

    Exposes the card at :data:`CAPABILITY_RESOURCE_URI` so any client can read
    the server's live transports, auth modes, active tier, and meter state
    without a server restart. The card is rebuilt on each read so it reflects
    current process state.

    Args:
        mcp: The FastMCP server to register the resource on.
    """

    @mcp.resource(
        CAPABILITY_RESOURCE_URI,
        name="bernstein_capability",
        description="Runtime capability card: transports, auth, tiers, meter, spec rev.",
        mime_type="application/json",
    )
    def capability_card() -> str:  # pyright: ignore[reportUnusedFunction]
        return json.dumps(build_capability_card(), sort_keys=True)
