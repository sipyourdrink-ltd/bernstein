"""SCIM 2.0 read-only service-provider surface (issue #5040, slice 1).

The endpoints under ``/scim/v2`` let an operator's identity system read the
agent principals this orchestrator knows about using the SCIM client it
already owns.  Slice 1 serves discovery and ``GET /Users`` only, so the tests
here hold two properties that a later write slice must not quietly break:

* ``ServiceProviderConfig`` describes the surface that is actually mounted -
  every operation it advertises has a route, and every operation it denies
  has none.  A discovery document that over-promises sends a real SCIM client
  into requests that 405.
* A credential scoped to provisioning reaches the provisioning surface and
  nothing else, checked against the whole registered route table rather than
  a hand-picked list, so a route added tomorrow is covered today.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient

from bernstein.core.routes.route_table import iter_route_paths
from bernstein.core.routes.scim import (
    SCIM_BASE_PATH,
    SCIM_MEDIA_TYPE,
    SCIM_PERM_READ,
    SCIM_PERM_WRITE,
    SCIM_SPC_EXTENSION,
    SCIM_USER_EXTENSION,
)
from bernstein.core.security.auth_middleware import (
    AUTH_PUBLIC_PATHS,
    _get_required_permission,
)
from bernstein.core.security.rbac import RBACEnforcer
from bernstein.core.server import create_app

if TYPE_CHECKING:
    from collections.abc import Iterator

_MUTATING_METHODS = ("POST", "PUT", "PATCH", "DELETE")

_SCIM_MODULE = Path(__file__).resolve().parents[3] / "src" / "bernstein" / "core" / "routes" / "scim.py"

#: Import roots the SCIM module may reach for.  Anything else is a vendor SDK
#: creeping into ``bernstein.core``, which the directory story forbids: SCIM is
#: JSON over HTTP and needs no client library.
_ALLOWED_IMPORT_ROOTS = frozenset(
    {
        "__future__",
        "bernstein",
        "collections",
        "datetime",
        "fastapi",
        "typing",
    }
)


@pytest.fixture()
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("BERNSTEIN_AUTH_DISABLED", "1")
    return create_app(jsonl_path=tmp_path / "runtime" / "tasks.jsonl")


@pytest.fixture()
def client(app: Any) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


class _ScopedPrincipal:
    """A credential holding exactly the permissions it was handed."""

    def __init__(self, *permissions: str) -> None:
        self._permissions = frozenset(permissions)
        self.role = "provisioning"

    def has_permission(self, permission: str) -> bool:
        return permission in self._permissions


def _mounted_methods(app: Any, prefix: str) -> set[str]:
    """Return every HTTP method mounted under *prefix*."""
    methods: set[str] = set()
    for path, route in iter_route_paths(app):
        if path.startswith(prefix):
            methods |= set(getattr(route, "methods", ()) or ())
    return methods


def _create_identity(app: Any, identity_id: str, *, role: str = "backend") -> Any:
    store = app.state.identity_store
    identity, _token = store.create_identity(session_id=identity_id, role=role)
    return identity


# ---------------------------------------------------------------------------
# 1. ServiceProviderConfig truthfulness
# ---------------------------------------------------------------------------


def test_service_provider_config_advertises_supported_operations_truthfully(
    client: TestClient,
    app: Any,
) -> None:
    """Every capability the discovery document claims must have a route."""
    resp = client.get(f"{SCIM_BASE_PATH}/ServiceProviderConfig")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith(SCIM_MEDIA_TYPE)
    body = resp.json()

    assert body["schemas"] == ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"]

    mounted = _mounted_methods(app, SCIM_BASE_PATH)
    assert "GET" in mounted, "the read surface must be mounted"

    # PATCH is RFC 7644 §3.5.2 and belongs to a later slice: not advertised,
    # and not mounted.
    assert body["patch"]["supported"] is False
    assert "PATCH" not in mounted

    # Nothing else is built either, and nothing else is claimed.
    for capability in ("bulk", "filter", "changePassword", "sort", "etag"):
        assert body[capability]["supported"] is False, capability

    extension = body[SCIM_SPC_EXTENSION]
    assert extension["resourceMutability"] == "read-only"
    assert not (mounted & set(_MUTATING_METHODS)), (
        f"ServiceProviderConfig says the surface is read-only but {sorted(mounted & set(_MUTATING_METHODS))} is mounted"
    )

    schemes = body["authenticationSchemes"]
    assert [scheme["type"] for scheme in schemes] == ["oauthbearertoken"]


def test_service_provider_config_declares_delete_as_soft_and_history_retaining(
    client: TestClient,
) -> None:
    """The reconciliation between SCIM's delete and an append-only record is stated up front."""
    body = client.get(f"{SCIM_BASE_PATH}/ServiceProviderConfig").json()
    delete = body[SCIM_SPC_EXTENSION]["delete"]

    # Not built yet - slice 1 is read-only.
    assert delete["supported"] is False
    # But the semantics a client will meet are declared now, not discovered later.
    assert delete["semantics"] == "soft"
    assert delete["retainsHistory"] is True


# ---------------------------------------------------------------------------
# 2. Provisioning scope reaches provisioning and nothing else
# ---------------------------------------------------------------------------


def test_provisioning_token_cannot_reach_any_non_provisioning_route(app: Any) -> None:
    """A provisioning-scoped credential is refused by every non-SCIM route.

    Walked over the whole registered route table rather than a fixed list, so
    a route added later is covered without editing this test.
    """
    principal = _ScopedPrincipal(SCIM_PERM_READ, SCIM_PERM_WRITE)
    reachable: list[tuple[str, str]] = []

    for path, route in sorted(iter_route_paths(app), key=lambda item: item[0]):
        if "/scim" in path:
            continue
        if path in AUTH_PUBLIC_PATHS:
            # Public paths need no credential at all; they are not something
            # the provisioning scope unlocks.
            continue
        for method in sorted(set(getattr(route, "methods", ()) or ()) - {"HEAD", "OPTIONS"}):
            required = _get_required_permission(path, method)
            if required is not None and principal.has_permission(required):
                reachable.append((method, path))

    assert not reachable, f"provisioning-scoped credential reaches non-provisioning routes: {reachable}"


def test_provisioning_scope_does_reach_the_scim_surface(app: Any) -> None:
    """The negative test above would pass trivially if the scope opened nothing."""
    principal = _ScopedPrincipal(SCIM_PERM_READ)
    scim_paths = [path for path, _ in iter_route_paths(app) if path.startswith(SCIM_BASE_PATH)]
    assert scim_paths, "no SCIM routes are mounted"

    for path in scim_paths:
        required = _get_required_permission(path, "GET")
        assert required is not None
        assert principal.has_permission(required), f"{path} is unreachable for the provisioning scope"


def test_scim_route_permission_is_declared_in_the_rbac_route_table() -> None:
    """The declarative rule table and the enforcing middleware must not disagree."""
    enforcer = RBACEnforcer()
    assert enforcer.get_required_permission(f"{SCIM_BASE_PATH}/Users", "GET") == SCIM_PERM_READ
    assert enforcer.get_required_permission(f"{SCIM_BASE_PATH}/Users", "POST") == SCIM_PERM_WRITE


def test_a_read_only_viewer_cannot_list_agent_principals() -> None:
    """SCIM /Users exposes principal records; ``status:read`` must not open it."""
    viewer = _ScopedPrincipal("status:read", "tasks:read")
    required = _get_required_permission(f"{SCIM_BASE_PATH}/Users", "GET")
    assert required == SCIM_PERM_READ
    assert not viewer.has_permission(required)


# ---------------------------------------------------------------------------
# 3. Schema / ResourceType discovery describes what is served
# ---------------------------------------------------------------------------


def test_user_schema_matches_the_attributes_actually_projected(client: TestClient, app: Any) -> None:
    """An advertised schema attribute must appear on a real resource, and vice versa."""
    _create_identity(app, "agent-schema-check")

    schemas = {entry["id"]: entry for entry in client.get(f"{SCIM_BASE_PATH}/Schemas").json()["Resources"]}
    user = client.get(f"{SCIM_BASE_PATH}/Users").json()["Resources"][0]

    core_id = "urn:ietf:params:scim:schemas:core:2.0:User"
    advertised = {attr["name"] for attr in schemas[core_id]["attributes"]}
    # ``id``, ``schemas`` and ``meta`` are SCIM common attributes (RFC 7643
    # §3.1), carried outside the schema's attribute list.
    projected = set(user) - {"id", "schemas", "meta", SCIM_USER_EXTENSION}
    assert advertised == projected

    ext_advertised = {attr["name"] for attr in schemas[SCIM_USER_EXTENSION]["attributes"]}
    assert ext_advertised == set(user[SCIM_USER_EXTENSION])


def test_resource_types_only_describe_resources_that_have_an_endpoint(client: TestClient, app: Any) -> None:
    """No ``Group`` resource type until a ``/Groups`` route exists."""
    resource_types = client.get(f"{SCIM_BASE_PATH}/ResourceTypes").json()["Resources"]
    mounted = {path for path, _ in iter_route_paths(app)}

    assert resource_types, "discovery must describe at least the User resource"
    for entry in resource_types:
        assert SCIM_BASE_PATH + entry["endpoint"] in mounted, entry["endpoint"]


def test_unknown_schema_id_returns_a_scim_error_document(client: TestClient) -> None:
    """RFC 7644 §3.12 error envelope, not FastAPI's default ``detail`` body."""
    resp = client.get(f"{SCIM_BASE_PATH}/Schemas/urn:example:nope")
    assert resp.status_code == 404
    body = resp.json()
    assert body["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:Error"]
    assert body["status"] == "404"


# ---------------------------------------------------------------------------
# 4. GET /Users
# ---------------------------------------------------------------------------


def test_get_users_projects_every_agent_principal_into_the_scim_list_envelope(
    client: TestClient,
    app: Any,
) -> None:
    _create_identity(app, "agent-alpha", role="backend")
    _create_identity(app, "agent-beta", role="qa")

    body = client.get(f"{SCIM_BASE_PATH}/Users").json()
    assert body["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:ListResponse"]
    assert body["totalResults"] == 2
    assert body["startIndex"] == 1
    assert body["itemsPerPage"] == 2

    by_id = {resource["id"]: resource for resource in body["Resources"]}
    assert set(by_id) == {"agent-alpha", "agent-beta"}
    assert by_id["agent-alpha"]["active"] is True
    assert by_id["agent-beta"][SCIM_USER_EXTENSION]["role"] == "qa"
    assert by_id["agent-beta"]["meta"]["resourceType"] == "User"


def test_get_users_pagination_reports_the_total_not_the_page_size(client: TestClient, app: Any) -> None:
    for index in range(3):
        _create_identity(app, f"agent-{index}")

    body = client.get(f"{SCIM_BASE_PATH}/Users", params={"startIndex": 2, "count": 1}).json()
    assert body["totalResults"] == 3
    assert body["startIndex"] == 2
    assert body["itemsPerPage"] == 1
    assert len(body["Resources"]) == 1


def test_get_single_user_returns_404_for_an_unknown_principal(client: TestClient) -> None:
    resp = client.get(f"{SCIM_BASE_PATH}/Users/nobody")
    assert resp.status_code == 404
    assert resp.json()["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:Error"]


def test_unsupported_filter_is_refused_rather_than_silently_ignored(client: TestClient) -> None:
    """``filter.supported`` is false, so a filtered query must not return an unfiltered list."""
    resp = client.get(f"{SCIM_BASE_PATH}/Users", params={"filter": 'userName eq "agent-alpha"'})
    assert resp.status_code == 501
    assert resp.json()["scimType"] == "invalidFilter"


# ---------------------------------------------------------------------------
# 5. No vendor SDK under bernstein.core
# ---------------------------------------------------------------------------


def test_scim_module_imports_no_vendor_sdk() -> None:
    """SCIM is JSON over HTTP; ``bernstein.core`` must not grow a client library."""
    tree = ast.parse(_SCIM_MODULE.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {alias.name.split(".", 1)[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".", 1)[0])

    assert roots <= _ALLOWED_IMPORT_ROOTS, f"unexpected imports in scim.py: {sorted(roots - _ALLOWED_IMPORT_ROOTS)}"
