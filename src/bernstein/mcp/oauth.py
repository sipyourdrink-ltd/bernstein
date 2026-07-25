"""OAuth-2 / OIDC discovery metadata for the Bernstein MCP server.

Bernstein is the **resource server**, not the authorization server. The only
discovery document Bernstein itself can correctly publish is the
protected-resource metadata (RFC 9728 / MCP draft) under
``/.well-known/oauth-protected-resource``. That document points clients at
the configured authorization server via ``authorization_servers[0]``; the
client then fetches the AS's own RFC 8414 metadata from the AS to learn
the per-IdP authorization, token, and JWKS endpoints (Keycloak, Auth0,
Okta, ... all use different layouts).

An earlier revision of this module also synthesised an RFC 8414
authorization-server metadata document under
``/.well-known/oauth-authorization-server`` with hardcoded ``/oauth/...``
paths under the issuer URL. That was incorrect: only the AS itself can
publish that document, and the path layout is operator-specific. The
fabricated metadata has been removed; clients should rely solely on the
protected-resource metadata to locate the IdP.

When the issuer env var is not set, the discovery endpoint returns 404 so
anonymous / static-bearer flows remain the only advertised path.
"""

from __future__ import annotations

import os
from typing import Any

#: Env var that configures the OAuth-2 issuer URL Bernstein advertises.
#: When unset, discovery endpoints return 404.
ISSUER_ENV: str = "BERNSTEIN_MCP_OAUTH_ISSUER"

#: Env var holding the comma-separated scopes the resource server accepts.
SCOPES_ENV: str = "BERNSTEIN_MCP_OAUTH_SCOPES"

#: Default scopes advertised when ``BERNSTEIN_MCP_OAUTH_SCOPES`` is unset.
_DEFAULT_SCOPES: tuple[str, ...] = ("bernstein.read", "bernstein.write")

#: Path where protected-resource metadata is served (MCP draft / RFC 9728).
PR_METADATA_PATH: str = "/.well-known/oauth-protected-resource"


def issuer() -> str:
    """Return the configured OAuth-2 issuer URL, or an empty string."""
    return os.environ.get(ISSUER_ENV, "").strip().rstrip("/")


def scopes_supported() -> list[str]:
    """Return the scopes advertised by the protected-resource metadata."""
    raw = os.environ.get(SCOPES_ENV, "").strip()
    if not raw:
        return list(_DEFAULT_SCOPES)
    return [s.strip() for s in raw.split(",") if s.strip()]


def oauth_discovery_enabled() -> bool:
    """Return True when the OAuth issuer is configured."""
    return bool(issuer())


def protected_resource_metadata(resource_url: str) -> dict[str, Any] | None:
    """Build the protected-resource metadata document (RFC 9728 / MCP draft).

    Args:
        resource_url: Absolute URL of the MCP resource (the streamable HTTP
            transport endpoint), used as the ``resource`` field.

    Returns:
        A JSON-serialisable dict, or ``None`` when no issuer is configured.
    """
    iss = issuer()
    if not iss:
        return None
    return {
        "resource": resource_url,
        "authorization_servers": [iss],
        "scopes_supported": scopes_supported(),
        "bearer_methods_supported": ["header"],
        "resource_documentation": "https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/mcp/server.md",
    }


def protected_resource_metadata_url(base_url: str) -> str:
    """Return the absolute URL of the protected-resource metadata document.

    Args:
        base_url: Scheme and authority the client reached the server on, for
            example ``https://bernstein.example.com``.

    Returns:
        The absolute URL the transport serves the metadata document at.
    """
    return f"{base_url.rstrip('/')}{PR_METADATA_PATH}"


def www_authenticate_challenge(base_url: str) -> str | None:
    """Build the ``WWW-Authenticate`` value for an unauthenticated request.

    A bare 401 leaves the protected-resource metadata undiscoverable: the
    client has no path from the refusal to the document. The challenge names
    the document's absolute URL so a client can start the authorization flow
    from the refusal alone.

    The value carries nothing but the metadata URL. No token, tenant, user, or
    issuer identifier is included, so the challenge is safe to return to a
    caller that has not authenticated.

    Args:
        base_url: Scheme and authority the client reached the server on.

    Returns:
        The header value, or ``None`` when no issuer is configured, in which
        case the anonymous and static-bearer flows are the only advertised
        paths and no challenge should be emitted.
    """
    if not oauth_discovery_enabled():
        return None
    return f'Bearer resource_metadata="{protected_resource_metadata_url(base_url)}"'


def capability_card_oauth() -> dict[str, Any]:
    """Return the ``auth.oauth`` subtree for the runtime capability card.

    Reports whether the discovery surface is live and which issuer it points
    at, so a client that fetches the card can locate the protected-resource
    metadata document without probing the well-known path. The card does
    not advertise an authorization-server metadata path: Bernstein is the
    resource server and the client follows ``authorization_servers[0]`` to
    the IdP's own RFC 8414 metadata.
    """
    iss = issuer()
    return {
        "enabled": bool(iss),
        "issuer": iss,
        "protectedResourceMetadata": PR_METADATA_PATH,
        "envVar": ISSUER_ENV,
    }
