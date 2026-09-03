"""SCIM 2.0 service-provider endpoints - read-only discovery and ``GET /Users``.

An operator's identity system already owns a SCIM client. Speaking SCIM 2.0
(RFC 7643 schema, RFC 7644 protocol) means that client can read the agent
principals this orchestrator knows about without anyone writing an adapter,
and without a vendor SDK reaching ``bernstein.core`` - SCIM is JSON over HTTP.

Endpoints
---------
``GET /scim/v2/ServiceProviderConfig``
    RFC 7643 §5 discovery document. It describes the surface that is actually
    mounted: a capability listed as supported here has a route, and one listed
    as unsupported has none.

``GET /scim/v2/Schemas`` / ``GET /scim/v2/Schemas/{id}``
    RFC 7643 §7 schema resources for the attributes this server projects. The
    core ``User`` schema advertises only the attributes a resource actually
    carries, so discovery cannot drift from the projection.

``GET /scim/v2/ResourceTypes`` / ``GET /scim/v2/ResourceTypes/{id}``
    RFC 7643 §6. Only resources with a mounted endpoint are described - there
    is no ``Group`` entry until there is a ``/Groups`` route.

``GET /scim/v2/Users`` / ``GET /scim/v2/Users/{id}``
    Agent principals projected into SCIM ``User`` resources, in the RFC 7644
    §3.4.2 ``ListResponse`` envelope.

Deletion semantics
------------------
SCIM clients expect ``DELETE`` to remove a resource. The record this server
keeps is append-only, so a principal removed upstream becomes inactive here
while the record of its existence and of its removal stays. That reconciliation
is declared in ``ServiceProviderConfig`` up front rather than discovered by a
client after the fact, even though the write surface itself is not built yet.

Access
------
Reads require ``scim:read`` and writes ``scim:write``, named in the route
tables in :mod:`bernstein.core.security.rbac` and
:mod:`bernstein.core.security.auth_middleware`. There is no separate auth path
here: the server-wide middleware resolves the requirement like any other route.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from bernstein.core.routes.identities import identity_store_for_request

if TYPE_CHECKING:
    from collections.abc import Iterable

router = APIRouter(tags=["scim"])

#: Mount point for the SCIM service provider. RFC 7644 §3.13 recommends a
#: ``/v2`` path segment so a future major version can be served alongside.
SCIM_BASE_PATH = "/scim/v2"

#: RFC 7644 §3.1 media type. Clients content-negotiate on it.
SCIM_MEDIA_TYPE = "application/scim+json"

#: Permission a caller needs to read the provisioning surface.
SCIM_PERM_READ = "scim:read"

#: Permission a caller needs to change it. No route requires it yet; it exists
#: so a write route added later cannot fall back to a read permission.
SCIM_PERM_WRITE = "scim:write"

_SCHEMA_SERVICE_PROVIDER_CONFIG = "urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"
_SCHEMA_RESOURCE_TYPE = "urn:ietf:params:scim:schemas:core:2.0:ResourceType"
_SCHEMA_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Schema"
_SCHEMA_USER = "urn:ietf:params:scim:schemas:core:2.0:User"
_SCHEMA_LIST_RESPONSE = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
_SCHEMA_ERROR = "urn:ietf:params:scim:api:messages:2.0:Error"

#: Extension namespace carrying the deletion semantics and the mutability of
#: this surface. RFC 7643 §5 fixes the ServiceProviderConfig attributes, so a
#: statement it has no field for belongs under an extension URN.
SCIM_SPC_EXTENSION = "urn:ietf:params:scim:schemas:extension:bernstein:2.0:ServiceProviderConfig"

#: Extension namespace for the agent-specific attributes of a principal.
SCIM_USER_EXTENSION = "urn:ietf:params:scim:schemas:extension:bernstein:2.0:AgentPrincipal"

_DOCUMENTATION_URI = "https://github.com/sipyourdrink-ltd/bernstein"

#: Largest page a client can request, so a wide ``count`` cannot turn one
#: request into an unbounded read of the principal store.
_MAX_PAGE_SIZE = 200


# ---------------------------------------------------------------------------
# Response plumbing
# ---------------------------------------------------------------------------


class _SCIMResponse(JSONResponse):
    """JSON response carrying the SCIM media type."""

    media_type = SCIM_MEDIA_TYPE


def _scim_error(status: int, detail: str, *, scim_type: str = "") -> _SCIMResponse:
    """Return an RFC 7644 §3.12 error document.

    FastAPI's default error body is ``{"detail": ...}``, which a SCIM client
    cannot parse as an error, so error paths build the envelope explicitly.
    """
    body: dict[str, Any] = {"schemas": [_SCHEMA_ERROR], "status": str(status), "detail": detail}
    if scim_type:
        body["scimType"] = scim_type
    return _SCIMResponse(status_code=status, content=body)


def _base_url(request: Request) -> str:
    """Return the absolute origin this request arrived on, without a trailing slash."""
    return str(request.base_url).rstrip("/")


def _location(request: Request, path: str) -> str:
    """Return the absolute ``meta.location`` for a resource under this router."""
    return f"{_base_url(request)}{request.scope.get('root_path', '')}{SCIM_BASE_PATH}{path}"


def _iso8601(timestamp: float) -> str:
    """Render a unix timestamp as the XML-schema dateTime SCIM ``meta`` uses."""
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat().replace("+00:00", "Z")


def _list_response(resources: list[dict[str, Any]], *, total: int, start_index: int) -> dict[str, Any]:
    """Wrap *resources* in the RFC 7644 §3.4.2 ``ListResponse`` envelope."""
    return {
        "schemas": [_SCHEMA_LIST_RESPONSE],
        "totalResults": total,
        "startIndex": start_index,
        "itemsPerPage": len(resources),
        "Resources": resources,
    }


# ---------------------------------------------------------------------------
# Schema definitions - the single source for both discovery and projection
# ---------------------------------------------------------------------------


def _attribute(
    name: str,
    *,
    description: str,
    attr_type: str = "string",
    uniqueness: str = "none",
) -> dict[str, Any]:
    """Build one RFC 7643 §7 attribute definition.

    Every attribute this server serves is ``readOnly``: the write slices are
    not built, and advertising a mutable attribute would invite a ``PUT`` that
    can only 405.
    """
    return {
        "name": name,
        "type": attr_type,
        "multiValued": False,
        "description": description,
        "required": False,
        "caseExact": False,
        "mutability": "readOnly",
        "returned": "default",
        "uniqueness": uniqueness,
    }


#: Core ``User`` attributes this server projects. The list is deliberately a
#: subset of RFC 7643 §4.1: a schema resource describes what the provider
#: serves, and advertising ``name``/``emails``/``phoneNumbers`` for an agent
#: principal that has none would be a false promise.
_USER_ATTRIBUTES: tuple[dict[str, Any], ...] = (
    _attribute("userName", description="Unique identifier for the principal.", uniqueness="server"),
    _attribute("displayName", description="Human-readable label for the principal."),
    _attribute(
        "active",
        description="False once the principal has been suspended or revoked here.",
        attr_type="boolean",
    ),
)

#: Agent-specific attributes, carried under the extension URN so a generic
#: SCIM client can ignore them and a directory-aware one can read them.
_AGENT_PRINCIPAL_ATTRIBUTES: tuple[dict[str, Any], ...] = (
    _attribute("role", description="Agent role this principal was minted for."),
    _attribute("sessionId", description="Agent session this principal belongs to."),
    _attribute("status", description="Lifecycle status: active, suspended, or revoked."),
)


def _schema_resource(
    schema_id: str,
    name: str,
    description: str,
    attributes: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Build one RFC 7643 §7 schema resource."""
    return {
        "schemas": [_SCHEMA_SCHEMA],
        "id": schema_id,
        "name": name,
        "description": description,
        "attributes": list(attributes),
    }


_SCHEMAS: tuple[dict[str, Any], ...] = (
    _schema_resource(
        _SCHEMA_USER,
        "User",
        "An agent principal known to this orchestrator.",
        _USER_ATTRIBUTES,
    ),
    _schema_resource(
        SCIM_USER_EXTENSION,
        "AgentPrincipal",
        "Agent-specific attributes of a principal.",
        _AGENT_PRINCIPAL_ATTRIBUTES,
    ),
)

#: Resource types this server serves. An entry here must have a mounted
#: endpoint - ``Group`` arrives with ``/Groups``, not before.
_RESOURCE_TYPES: tuple[dict[str, Any], ...] = (
    {
        "schemas": [_SCHEMA_RESOURCE_TYPE],
        "id": "User",
        "name": "User",
        "endpoint": "/Users",
        "description": "Agent principals known to this orchestrator.",
        "schema": _SCHEMA_USER,
        "schemaExtensions": [{"schema": SCIM_USER_EXTENSION, "required": False}],
    },
)


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------


def _principal_sort_key(identity: Any) -> str:
    """Order principals by id so a page boundary is stable across requests."""
    return str(identity.id)


def _scim_user(identity: Any, request: Request) -> dict[str, Any]:
    """Project one agent identity into a SCIM ``User`` resource."""
    last_modified = identity.revoked_at or identity.last_authenticated_at or identity.created_at
    return {
        "schemas": [_SCHEMA_USER, SCIM_USER_EXTENSION],
        "id": identity.id,
        "userName": identity.id,
        "displayName": identity.role,
        "active": bool(identity.is_active),
        SCIM_USER_EXTENSION: {
            "role": identity.role,
            "sessionId": identity.session_id,
            "status": identity.status.value,
        },
        "meta": {
            "resourceType": "User",
            "created": _iso8601(identity.created_at),
            "lastModified": _iso8601(last_modified),
            "location": _location(request, f"/Users/{identity.id}"),
        },
    }


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


@router.get(
    f"{SCIM_BASE_PATH}/ServiceProviderConfig",
    summary="SCIM 2.0 service provider configuration",
)
def service_provider_config(request: Request) -> _SCIMResponse:
    """Return the RFC 7643 §5 discovery document for this service provider.

    Everything reported here is what the mounted surface actually does. The
    write operations are absent rather than advertised-and-unimplemented, and
    the deletion semantics a client will eventually meet are stated now.
    """
    return _SCIMResponse(
        content={
            "schemas": [_SCHEMA_SERVICE_PROVIDER_CONFIG],
            "documentationUri": _DOCUMENTATION_URI,
            "patch": {"supported": False},
            "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
            "filter": {"supported": False, "maxResults": 0},
            "changePassword": {"supported": False},
            "sort": {"supported": False},
            "etag": {"supported": False},
            "authenticationSchemes": [
                {
                    "type": "oauthbearertoken",
                    "name": "OAuth Bearer Token",
                    "description": (
                        "Bearer token in the Authorization header, authorised against the "
                        "scim:read permission by the server-wide auth middleware."
                    ),
                    "specUri": "https://www.rfc-editor.org/rfc/rfc6750",
                    "primary": True,
                }
            ],
            SCIM_SPC_EXTENSION: {
                "resourceMutability": "read-only",
                "delete": {
                    "supported": False,
                    "semantics": "soft",
                    "retainsHistory": True,
                    "description": (
                        "When deletion is served it will mark the principal inactive and "
                        "answer 204. The record of the principal and of its removal is "
                        "retained, so a question about a past date can still be answered."
                    ),
                },
            },
            "meta": {
                "resourceType": "ServiceProviderConfig",
                "location": _location(request, "/ServiceProviderConfig"),
            },
        }
    )


@router.get(f"{SCIM_BASE_PATH}/Schemas", summary="SCIM 2.0 schema resources")
def list_schemas(request: Request) -> _SCIMResponse:
    """List the schema resources this server serves (RFC 7643 §7)."""
    resources = [_with_schema_meta(schema, request) for schema in _SCHEMAS]
    return _SCIMResponse(content=_list_response(resources, total=len(resources), start_index=1))


@router.get(
    f"{SCIM_BASE_PATH}/Schemas/{{schema_id}}",
    summary="Fetch a single SCIM schema resource",
    responses={404: {"description": "Unknown schema URN"}},
)
def get_schema(schema_id: str, request: Request) -> _SCIMResponse:
    """Fetch one schema resource by its URN."""
    for schema in _SCHEMAS:
        if schema["id"] == schema_id:
            return _SCIMResponse(content=_with_schema_meta(schema, request))
    return _scim_error(404, f"Schema {schema_id} is not served by this provider")


@router.get(f"{SCIM_BASE_PATH}/ResourceTypes", summary="SCIM 2.0 resource types")
def list_resource_types(request: Request) -> _SCIMResponse:
    """List the resource types this server serves (RFC 7643 §6)."""
    resources = [_with_resource_type_meta(entry, request) for entry in _RESOURCE_TYPES]
    return _SCIMResponse(content=_list_response(resources, total=len(resources), start_index=1))


@router.get(
    f"{SCIM_BASE_PATH}/ResourceTypes/{{resource_type_id}}",
    summary="Fetch a single SCIM resource type",
    responses={404: {"description": "Unknown resource type"}},
)
def get_resource_type(resource_type_id: str, request: Request) -> _SCIMResponse:
    """Fetch one resource type by id."""
    for entry in _RESOURCE_TYPES:
        if entry["id"] == resource_type_id:
            return _SCIMResponse(content=_with_resource_type_meta(entry, request))
    return _scim_error(404, f"Resource type {resource_type_id} is not served by this provider")


def _with_schema_meta(schema: dict[str, Any], request: Request) -> dict[str, Any]:
    return {**schema, "meta": {"resourceType": "Schema", "location": _location(request, f"/Schemas/{schema['id']}")}}


def _with_resource_type_meta(entry: dict[str, Any], request: Request) -> dict[str, Any]:
    return {
        **entry,
        "meta": {"resourceType": "ResourceType", "location": _location(request, f"/ResourceTypes/{entry['id']}")},
    }


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


@router.get(
    f"{SCIM_BASE_PATH}/Users",
    summary="List agent principals as SCIM users",
    responses={501: {"description": "Filtering is not supported by this provider"}},
)
def list_users(
    request: Request,
    startIndex: int = 1,  # RFC 7644 §3.4.2.4 names the query parameter
    count: int | None = None,
    filter: str | None = None,  # RFC 7644 §3.4.2.2 names the query parameter
) -> _SCIMResponse:
    """Return the agent principals this orchestrator knows about.

    ``ServiceProviderConfig`` reports ``filter.supported = false``, so a
    ``filter`` query is refused rather than ignored: silently returning the
    unfiltered list would hand a client more principals than it asked for and
    look like a successful narrow query.
    """
    if filter is not None:
        return _scim_error(
            501,
            "This provider does not support filtering; see ServiceProviderConfig.",
            scim_type="invalidFilter",
        )

    store: Any = identity_store_for_request(request)
    identities: list[Any] = sorted(store.list_identities(), key=_principal_sort_key)

    start = max(startIndex, 1)
    page_size = len(identities) if count is None else max(count, 0)
    page = identities[start - 1 : start - 1 + min(page_size, _MAX_PAGE_SIZE)]

    return _SCIMResponse(
        content=_list_response(
            [_scim_user(identity, request) for identity in page],
            total=len(identities),
            start_index=start,
        )
    )


@router.get(
    f"{SCIM_BASE_PATH}/Users/{{user_id}}",
    summary="Fetch one agent principal as a SCIM user",
    responses={404: {"description": "Unknown principal"}},
)
def get_user(user_id: str, request: Request) -> _SCIMResponse:
    """Fetch a single agent principal by its SCIM ``id``."""
    identity = identity_store_for_request(request).get(user_id)
    if identity is None:
        return _scim_error(404, f"Principal {user_id} not found", scim_type="invalidValue")
    return _SCIMResponse(content=_scim_user(identity, request))
