"""Static service manifest routes - A2A v1.0 agent card, JWKS, llms.txt.

External agents (Claude Code, Codex, third-party orchestrators) discover the
Bernstein task API by fetching ``/.well-known/agent.json`` (A2A v1.0 card,
JCS-canonical body + detached JWS), ``/.well-known/agent.json/keys`` (JWKS
of the signing keys), or ``/llms.txt`` (markdown summary).

The structured manifest and the markdown summary derive from the same
in-module ``_ENDPOINTS`` table so the markdown summary cannot drift from
the structured manifest - the regression test in
``tests/unit/test_well_known.py`` enforces that every entry in the table is
mentioned in the rendered llms.txt body.

A2A v1.0 conformance
--------------------
- ``protocolVersion: "1.0"`` (RFC 8785 + RFC 7515 baseline).
- ``supportedInterfaces[]`` - the wire formats this server speaks.
- ``securitySchemes[]`` - Bearer JWT today, with a stub for the upcoming
  ``mtls`` scheme that ``auth_middleware.py`` will land in a follow-up.
- ``signatures[]`` - list of detached JWS objects (RFC 7515 §A.5) over the
  JCS-canonical body bytes (RFC 8785). Verifiers strip ``signatures`` from
  the body, recompute the canonical bytes, and verify the JWS using the
  matching ``kid`` from the JWKS endpoint.

Both routes are unauthenticated; they live in ``AUTH_PUBLIC_PATHS`` so any
network caller can read them without provisioning a token.

Key lifecycle
-------------
The signing keypair persists at ``.bernstein/keys/agent-card.ed25519`` (and
its ``.pub`` companion). On first request the keystore atomically mints the
key with ``O_EXCL`` and ``0o600`` permissions; subsequent requests (and
restarts) reuse it. Operators rotate via
:func:`bernstein.core.security.agent_card_keystore.AgentCardKeystore.rotate`,
which archives the previous keypair under
``.bernstein/keys/archive/<utc-isoformat>/`` and mints a new one.

During a rotation grace window (24h by default) the JWKS endpoint publishes
both the current and the archived public key so verifiers cached on the old
``kid`` keep validating until their HTTP cache (``max-age=3600`` on this
route) ages out.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse, Response

from bernstein import __version__ as _BERNSTEIN_VERSION
from bernstein.core.interop.a2a_card import (
    SignedCapabilityCard,
    issue_capability_card,
    resolve_advertised_card_policies,
)
from bernstein.core.security.agent_card_keystore import (
    DEFAULT_KEY_DIR,
    AgentCardKeystore,
)
from bernstein.core.security.agent_card_signer import (
    canonicalize_jcs,
    ed25519_public_jwk,
)
from bernstein.core.security.tenanting import DEFAULT_TENANT_ID

router = APIRouter()

#: Well-known path publishing the key directory (JWKS) that verifiers fetch to
#: validate outbound HTTP Message Signatures (issue #2305). The path follows
#: the Web Bot Auth convention so third-party sites can locate it. The keys
#: served here are the install-identity keypair - the same keystore used to
#: sign outbound agent-facing requests - each keyed by its RFC 7638 thumbprint.
HTTP_SIG_DIRECTORY_PATH = "/.well-known/http-message-signatures-directory"

_AGENT_NAME = "bernstein"
_AGENT_DESCRIPTION = (
    "Bernstein is a deterministic orchestrator for short-lived CLI coding "
    "agents (Claude Code, Codex, Gemini CLI, Aider, ...) over a file-based "
    "task store.  No model sits in its coordination loop, so parallel runs "
    "in per-task git worktrees replay byte-identically.  Clients submit "
    "tasks, query status, and post cross-agent bulletins via the documented "
    "endpoints below."
)
_PROTOCOL_VERSION = "1.0"
_DEFAULT_BASE_URL = "http://127.0.0.1:8052"
_DOCS_URL = "https://github.com/sipyourdrink-ltd/bernstein"

#: A2A v1.0 wire formats this server speaks. Today only HTTP+JSON; gRPC and
#: JSONRPC are tracked in follow-up tickets but listed here as the ticket
#: enumerates the v1.0 surface.
_SUPPORTED_INTERFACES: tuple[str, ...] = ("HTTP+JSON",)

#: Stable kid used for the orchestrator's signing key. Format follows the
#: convention in ``agent_card_signer.sign_agent_card``: ``agent-<id>``.
_DEFAULT_KID = "agent-bernstein-orchestrator"


@dataclass(frozen=True, slots=True)
class _Endpoint:
    """Single documented endpoint in the manifest."""

    method: str
    path: str
    summary: str


_ENDPOINTS: tuple[_Endpoint, ...] = (
    _Endpoint("POST", "/tasks", "Create a new task in the backlog."),
    _Endpoint("GET", "/tasks", "List tasks (filter via ?status=open|claimed|done)."),
    _Endpoint("GET", "/tasks/{id}", "Fetch a single task by id."),
    _Endpoint("POST", "/tasks/{id}/complete", "Mark task done with a result summary."),
    _Endpoint("POST", "/tasks/{id}/fail", "Mark task failed with an error reason."),
    _Endpoint("POST", "/tasks/{id}/progress", "Report partial progress (files, tests, errors)."),
    _Endpoint("POST", "/bulletin", "Post a finding or blocker visible to other agents."),
    _Endpoint("GET", "/bulletin", "Read recent bulletins (filter via ?since=ts)."),
    _Endpoint("GET", "/status", "Server-side dashboard summary."),
    _Endpoint("GET", "/health", "Liveness probe."),
    _Endpoint("GET", "/health/ready", "Readiness probe."),
)

_SKILLS: tuple[dict[str, object], ...] = (
    {
        "id": "task-orchestration",
        "name": "Task orchestration",
        "description": "Submit goals, watch their progress, and react to terminal state.",
        "tags": ["tasks", "orchestration"],
    },
    {
        "id": "agent-bulletin",
        "name": "Cross-agent bulletin",
        "description": "Broadcast findings and blockers to peer agents.",
        "tags": ["bulletin", "messaging"],
    },
)


# ---------------------------------------------------------------------------
# Persistent Ed25519 keystore + in-process cache.
# ---------------------------------------------------------------------------
#
# The orchestrator persists its signing keypair at ``.bernstein/keys/`` so
# the ``kid`` advertised in the JWKS stays stable across process restarts.
# The first GET lazily binds a process-wide :class:`AgentCardKeystore` to
# that directory; subsequent requests reuse the cached PEM bytes (loading
# from disk on every request would charge an unnecessary syscall per call).
# ``_reset_signing_keypair_for_tests`` drops the cache between test cases
# so each test can point at its own ``tmp_path`` keystore.

# Reentrant: ``_get_signing_keypair()`` holds the lock while delegating to
# ``_get_keystore()``, which on the cold path needs to take the same lock to
# bind the keystore -- a plain ``threading.Lock`` self-deadlocks on the very
# first JWKS call. ``rotate_agent_card_keys()`` has the same nesting shape.
#
# Per-tenant identities (#2525): a host serving several tenants must not
# advertise one shared identity for all of them. Key material for a tenant
# lives under ``<key_dir>/tenants/<tenant-id>/``; the default (single-tenant)
# identity keeps living at ``<key_dir>`` for backward compatibility. The caches
# below are keyed by normalised tenant id so each tenant resolves its own
# keystore, keypair, and ``kid``.
_KEY_LOCK = threading.RLock()
_KEYSTORES: dict[str, AgentCardKeystore] = {}
_KEYPAIRS: dict[str, tuple[bytes, bytes]] = {}
#: Per-tenant cache of the issued capability card (#2609). Cards carry
#: ``created_at`` / ``expires_at``, so re-issuing per request would make the
#: identity surface churn and defeat the route's ``Cache-Control``. The card
#: is minted once per tenant and re-minted only once it nears expiry.
_CAPABILITY_CARDS: dict[str, SignedCapabilityCard] = {}
#: Test-only override for the base key directory (set by
#: ``_reset_signing_keypair_for_tests``). ``None`` means "use the resolved
#: production directory".
_KEY_DIR_OVERRIDE: Path | None = None


def _resolve_key_dir() -> Path:
    """Return the base directory backing the persistent keystore.

    Honours ``BERNSTEIN_AGENT_CARD_KEY_DIR`` so production deployments can
    point at a mounted secret volume; falls back to ``.bernstein/keys`` in
    the working directory. A test override (set via
    ``_reset_signing_keypair_for_tests``) takes precedence.
    """
    if _KEY_DIR_OVERRIDE is not None:
        return _KEY_DIR_OVERRIDE
    override = os.environ.get("BERNSTEIN_AGENT_CARD_KEY_DIR", "").strip()
    if override:
        return Path(override)
    return DEFAULT_KEY_DIR


def _normalize_tenant(tenant_id: str | None) -> str:
    """Return the normalised tenant id (empty / whitespace -> ``default``)."""
    from bernstein.core.security.tenanting import normalize_tenant_id

    return normalize_tenant_id(tenant_id)


def _tenant_key_dir(tenant_id: str) -> Path:
    """Return the key directory for ``tenant_id``.

    The default tenant keeps the historical top-level directory so existing
    single-tenant deployments and their cached JWKs are untouched; every other
    tenant gets an isolated ``tenants/<tenant-id>/`` subtree so one tenant's key
    exposure never implicates another.
    """
    base = _resolve_key_dir()
    if tenant_id == DEFAULT_TENANT_ID:
        return base
    return base / "tenants" / tenant_id


def _tenant_kid(tenant_id: str) -> str:
    """Return the stable ``kid`` advertised for ``tenant_id``.

    The default tenant keeps ``agent-bernstein-orchestrator`` so verifiers that
    cached the single-tenant key still route by it; other tenants append their
    id so a card and its JWKS resolve within the tenant only.
    """
    if tenant_id == DEFAULT_TENANT_ID:
        return _DEFAULT_KID
    return f"{_DEFAULT_KID}-{tenant_id}"


def _get_keystore(tenant_id: str = DEFAULT_TENANT_ID) -> AgentCardKeystore:
    """Return the per-tenant :class:`AgentCardKeystore`, creating it lazily."""
    tenant_id = _normalize_tenant(tenant_id)
    cached = _KEYSTORES.get(tenant_id)
    if cached is not None:
        return cached
    with _KEY_LOCK:
        cached = _KEYSTORES.get(tenant_id)
        if cached is None:
            cached = AgentCardKeystore(_tenant_key_dir(tenant_id))
            _KEYSTORES[tenant_id] = cached
    return cached


def _get_signing_keypair(tenant_id: str = DEFAULT_TENANT_ID) -> tuple[bytes, bytes]:
    """Return the cached per-tenant signing keypair, loading from disk on first use."""
    tenant_id = _normalize_tenant(tenant_id)
    cached = _KEYPAIRS.get(tenant_id)
    if cached is not None:
        return cached
    with _KEY_LOCK:
        cached = _KEYPAIRS.get(tenant_id)
        if cached is None:
            cached = _get_keystore(tenant_id).load_or_generate()
            _KEYPAIRS[tenant_id] = cached
    return cached


def _reset_signing_keypair_for_tests(key_dir: Path | None = None) -> None:
    """Reset every tenant keystore binding and cached keypair.

    Tests pass ``key_dir=tmp_path / "keys"`` so each case gets a fresh base
    directory (tenant subtrees derive from it); production callers leave
    ``key_dir=None`` (the resolved persistent directory is re-bound on next
    request).
    """
    global _KEY_DIR_OVERRIDE
    with _KEY_LOCK:
        _KEY_DIR_OVERRIDE = key_dir
        _KEYSTORES.clear()
        _KEYPAIRS.clear()
        _CAPABILITY_CARDS.clear()


def rotate_agent_card_keys(tenant_id: str = DEFAULT_TENANT_ID) -> tuple[bytes, bytes]:
    """Rotate the persistent agent-card keypair for ``tenant_id``.

    Archives the current keypair under ``<key_dir>/archive/<isoformat>/`` and
    mints a fresh one with ``O_EXCL`` + ``0o600`` semantics. The JWKS
    endpoint will continue to publish the rotated-out public key for the
    keystore's grace window (24h by default) so verifiers cached on the
    old ``kid`` keep validating until their HTTP cache ages out. Rotation is
    scoped to the tenant, so one tenant rotating never disturbs another.

    Returns:
        The freshly-generated ``(private_pem, public_pem)`` so callers can
        log the new ``kid`` or trigger downstream secret-store sync.
    """
    tenant_id = _normalize_tenant(tenant_id)
    with _KEY_LOCK:
        priv, pub = _get_keystore(tenant_id).rotate()
        _KEYPAIRS[tenant_id] = (priv, pub)
        # The cached capability card is signed with the retired key; drop it
        # so the next fetch re-issues under the new ``kid``.
        _CAPABILITY_CARDS.pop(tenant_id, None)
        return priv, pub


# ---------------------------------------------------------------------------
# Card body construction.
# ---------------------------------------------------------------------------


def _security_schemes() -> list[dict[str, Any]]:
    """Return the A2A v1.0 ``securitySchemes`` array.

    Today only ``Bearer`` is fully wired. ``mtls`` is listed as a stub
    (``"required": false``) because client-cert verification at the
    middleware layer is the next ticket in the same family - declaring it
    early lets external clients negotiate it as soon as it lands without a
    discovery-cache miss.
    """
    return [
        {
            "id": "bearer-jwt",
            "type": "http",
            "scheme": "Bearer",
            "description": "JWT bearer token in the Authorization header.",
            "required": True,
        },
        {
            "id": "mtls",
            "type": "mutualTLS",
            "scheme": "mtls",
            "description": "TLS client cert (deferred - declared for forward-compat).",
            "required": False,
        },
    ]


def _agent_card_body(base_url: str = _DEFAULT_BASE_URL, *, tenant_id: str = DEFAULT_TENANT_ID) -> dict[str, Any]:
    """Build the A2A v1.0 card body - the bytes the JWS attests to.

    The result excludes the ``signatures`` array; ``_agent_card_payload``
    appends the JWS list after JCS-canonicalising this body.

    Args:
        base_url: Public base URL of the task server.
        tenant_id: Tenant the card is served for. The default tenant's body is
            byte-identical to the historical single-tenant card; a named tenant
            adds a ``tenantId`` field so the served card is attributable to the
            tenant that requested it.

    Returns:
        JSON-serialisable dict with the v1.0-mandated fields.
    """
    body: dict[str, Any] = {
        "name": _AGENT_NAME,
        "description": _AGENT_DESCRIPTION,
        "version": _BERNSTEIN_VERSION,
        "protocolVersion": _PROTOCOL_VERSION,
        "url": base_url,
        "documentationUrl": _DOCS_URL,
        "supportedInterfaces": list(_SUPPORTED_INTERFACES),
        "securitySchemes": _security_schemes(),
        "capabilities": [
            {"name": "task-crud", "description": "Create / read / complete / fail tasks."},
            {"name": "bulletin", "description": "Post and read cross-agent bulletins."},
            {"name": "status", "description": "Read server status and health probes."},
        ],
        "skills": list(_SKILLS),
        "defaultInputModes": ["text", "application/json"],
        "defaultOutputModes": ["application/json"],
        "authentication": {
            "schemes": ["Bearer"],
            "publicPaths": [
                "/health",
                "/.well-known/agent.json",
                "/.well-known/agent.json/keys",
                "/llms.txt",
            ],
            "description": (
                "Bearer token in Authorization header.  Set BERNSTEIN_AUTH_DISABLED=1 "
                "for local development (no token required)."
            ),
        },
        "endpoints": [{"method": e.method, "path": e.path, "summary": e.summary} for e in _ENDPOINTS],
    }
    if tenant_id != DEFAULT_TENANT_ID:
        body["tenantId"] = tenant_id
    _apply_a2a_server_surface(body, base_url)
    return body


def _apply_a2a_server_surface(body: dict[str, Any], base_url: str) -> None:
    """Declare the inbound A2A JSON-RPC binding + auth in the card, if enabled.

    Off by default: when ``BERNSTEIN_A2A_SERVER_ENABLED`` is unset the card is
    byte-identical to the historical one, so existing verifiers and cached
    cards are undisturbed. When enabled, the card advertises

    * the JSON-RPC 2.0 interface (transport + endpoint URL) as an additional
      interface, and ``JSONRPC`` in ``supportedInterfaces``; and
    * the two callable-node auth schemes - a static API key and an OAuth2
      client-credentials grant with its token URL -

    so a peer negotiates the binding and credentials before sending a task.
    """
    # Local import keeps the route module (which imports FastAPI + task server
    # helpers) out of this module's import graph unless the surface is on.
    from bernstein.core.protocols.a2a.server_auth import A2AServerAuth
    from bernstein.core.routes.a2a_jsonrpc import (
        A2A_JSONRPC_PATH,
        A2A_TOKEN_PATH,
        a2a_server_enabled,
    )

    if not a2a_server_enabled():
        return

    rpc_url = f"{base_url.rstrip('/')}{A2A_JSONRPC_PATH}"
    token_url = f"{base_url.rstrip('/')}{A2A_TOKEN_PATH}"

    interfaces = body["supportedInterfaces"]
    if "JSONRPC" not in interfaces:
        interfaces.append("JSONRPC")
    body["additionalInterfaces"] = [{"url": rpc_url, "transport": "JSONRPC"}]

    body["securitySchemes"] = body["securitySchemes"] + A2AServerAuth.from_env().security_schemes(token_url=token_url)


def _sign_canonical_body(canonical_body: bytes, private_pem: bytes, *, kid: str) -> str:
    """Produce a detached JWS over ``canonical_body`` (RFC 7515 §A.5).

    Mirrors :func:`agent_card_signer.sign_agent_card` but operates on the
    raw canonical bytes - the agent card we publish here is a server-card
    (not an ``AgentIdentityCard`` instance), so we cannot reuse
    ``sign_agent_card`` directly without inventing a synthetic dataclass.
    The signing input shape (header.body) and ``typ`` value match exactly,
    so verifiers that already understand ``agent-card+jws`` interoperate.

    Args:
        canonical_body: JCS-canonicalised body bytes.
        private_pem: PEM PKCS#8 Ed25519 private key.
        kid: Key identifier - must match the JWK published at
            ``/.well-known/agent.json/keys``.

    Returns:
        Compact-form detached JWS string ``header..signature``.
    """
    import base64

    from cryptography.hazmat.primitives import serialization

    private_key = serialization.load_pem_private_key(private_pem, password=None)
    header = {"alg": "EdDSA", "typ": "agent-card+jws", "kid": kid}
    header_b64 = base64.urlsafe_b64encode(canonicalize_jcs(header)).rstrip(b"=").decode("ascii")
    body_b64 = base64.urlsafe_b64encode(canonical_body).rstrip(b"=").decode("ascii")
    signing_input = f"{header_b64}.{body_b64}".encode("ascii")
    signature = private_key.sign(signing_input)
    sig_b64 = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
    return f"{header_b64}..{sig_b64}"


#: Tools the node advertises as delegable over A2A. These are the coarse
#: capability names a peer matches against before it sends work; the
#: fine-grained endpoint list stays in ``_ENDPOINTS``.
_ADVERTISED_TOOLS: tuple[str, ...] = (
    "task_orchestration",
    "agent_spawning",
    "code_review",
    "a2a_message",
)

#: Re-issue the cached card once it is within this many seconds of expiry, so
#: a peer never fetches a card that expires mid-verification.
_CARD_REISSUE_MARGIN_SECONDS = 5 * 60


def _capability_card(tenant_id: str = DEFAULT_TENANT_ID) -> SignedCapabilityCard:
    """Return the tenant's signed capability card, minting it on demand.

    The card is signed with the same keypair that signs the A2A v1.0 card and
    carries the same ``kid``, so a peer resolves both claims against the one
    JWK set published at ``/.well-known/agent.json/keys``.

    The two claims stay separate on purpose. The v1.0 ``signatures[]`` block
    attests to the *card body* (who this server is and what it exposes); the
    capability card attests to the *terms of delegation* (which tools, under
    which cost / redaction / sandbox policy). A verifier that only understands
    v1.0 keeps working; one that understands capability cards gets a signed
    policy statement it can gate on before sending work.
    """
    tenant_id = _normalize_tenant(tenant_id)
    cached = _CAPABILITY_CARDS.get(tenant_id)
    if cached is not None and not cached.card.is_expired(now=time.time() + _CARD_REISSUE_MARGIN_SECONDS):
        return cached

    with _KEY_LOCK:
        cached = _CAPABILITY_CARDS.get(tenant_id)
        if cached is not None and not cached.card.is_expired(now=time.time() + _CARD_REISSUE_MARGIN_SECONDS):
            return cached
        private_pem, public_pem = _get_signing_keypair(tenant_id)
        signed, _private = issue_capability_card(
            issuer=_AGENT_NAME if tenant_id == DEFAULT_TENANT_ID else f"{_AGENT_NAME}-{tenant_id}",
            name=_AGENT_NAME,
            description=_AGENT_DESCRIPTION,
            advertised_tools=list(_ADVERTISED_TOOLS),
            policies=resolve_advertised_card_policies(),
            private_key_pem=private_pem,
            public_key_pem=public_pem,
            kid=_tenant_kid(tenant_id),
        )
        _CAPABILITY_CARDS[tenant_id] = signed
        return signed


def _resolve_base_url() -> str:
    """Return the base URL to advertise in the card.

    Priority: ``BERNSTEIN_PUBLIC_BASE_URL`` env override → default. Keeping
    this configurable lets reverse-proxied deployments expose the correct
    canonical URL without hardcoding it at build time.
    """
    return os.environ.get("BERNSTEIN_PUBLIC_BASE_URL", _DEFAULT_BASE_URL)


def _agent_card_payload(base_url: str = _DEFAULT_BASE_URL, *, tenant_id: str = DEFAULT_TENANT_ID) -> dict[str, Any]:
    """Build the full A2A v1.0 card payload - body plus signatures.

    Verifiers strip ``signatures`` from this payload, JCS-canonicalise the
    rest, and compare against the ``signatures[].jws`` header+sig segments
    using the public key fetched from ``/.well-known/agent.json/keys``.

    Args:
        base_url: Public base URL of the task server.
        tenant_id: Tenant the card is served for. The card is signed with the
            tenant's own keypair and carries the tenant's ``kid`` so it resolves
            only against that tenant's JWKS.

    Returns:
        Full v1.0 payload dict ready to JSON-serialise.
    """
    tenant_id = _normalize_tenant(tenant_id)
    body = _agent_card_body(base_url, tenant_id=tenant_id)
    # The capability card joins the body *before* canonicalisation, so the
    # v1.0 JWS covers it too. The v1.0 verifier contract is "strip
    # ``signatures``, canonicalise the rest, verify" - appending an extension
    # field afterwards would break every verifier that follows it, including
    # third-party ones we do not control. Signing it inside the body keeps
    # that contract intact and gives the card two independent signatures:
    # its own (``a2a-capability+jws``) and the enclosing v1.0 card's.
    body["capabilityCard"] = _capability_card(tenant_id).to_dict()
    canonical = canonicalize_jcs(body)
    private_pem, _public_pem = _get_signing_keypair(tenant_id)
    kid = _tenant_kid(tenant_id)
    jws = _sign_canonical_body(canonical, private_pem, kid=kid)
    body["signatures"] = [
        {
            "kid": kid,
            "alg": "EdDSA",
            "typ": "agent-card+jws",
            "jws": jws,
        }
    ]
    return body


def _resolve_tenant(request: Request | None) -> str:
    """Return the tenant a well-known request is scoped to.

    Resolution order: an explicit ``?tenant=`` query parameter, then the
    ``x-tenant-id`` request header, then the default tenant. Keeping the query
    parameter first makes the tenant card deterministically addressable without
    provisioning auth, which is what an external verifier needs. A direct call
    with ``None`` (in-process, no HTTP request) resolves to the default tenant.
    """
    if request is None:
        return DEFAULT_TENANT_ID
    query_tenant = request.query_params.get("tenant")
    if query_tenant and query_tenant.strip():
        return _normalize_tenant(query_tenant)
    return _normalize_tenant(request.headers.get("x-tenant-id"))


def _render_llms_txt() -> str:
    """Render the markdown summary served at /llms.txt."""
    lines: list[str] = [
        f"# {_AGENT_NAME}",
        "",
        f"> {_AGENT_DESCRIPTION}",
        "",
        f"- Version: {_BERNSTEIN_VERSION}",
        f"- Protocol: A2A {_PROTOCOL_VERSION}",
        f"- Docs: {_DOCS_URL}",
        "",
        "## Endpoints",
        "",
    ]
    lines.extend(f"- `{e.method} {e.path}` - {e.summary}" for e in _ENDPOINTS)
    lines += [
        "",
        "## Auth",
        "",
        "Send `Authorization: Bearer <token>` on every request.  Public paths: "
        "`/health`, `/.well-known/agent.json`, `/.well-known/agent.json/keys`, "
        "`/llms.txt`.",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _serve_agent_card(request: Request) -> Response:
    """Render the signed A2A v1.0 card as a JCS-canonical JSON response.

    Shared by ``/.well-known/agent.json`` and ``/.well-known/agent-card.json``
    so both paths return byte-identical, identically-signed bytes.
    """
    tenant_id = _resolve_tenant(request)
    payload = _agent_card_payload(_resolve_base_url(), tenant_id=tenant_id)
    body = canonicalize_jcs(payload)
    headers = {"Cache-Control": "public, max-age=3600"}
    if tenant_id != DEFAULT_TENANT_ID:
        # Cards vary by tenant selector, so caches must key on it.
        headers["Vary"] = "x-tenant-id"
    return Response(
        content=body,
        media_type="application/json",
        headers=headers,
    )


@router.get("/.well-known/agent.json", include_in_schema=False)
def agent_json(request: Request) -> Response:
    """Return the A2A v1.0 signed agent card for this task server.

    Body bytes are JCS-canonical (RFC 8785) so verifiers can recompute the
    JWS signing input bit-perfect after stripping the ``signatures`` array.
    Cache for an hour - the card body changes only when the server config
    or the orchestrator's signing key rotates. A ``?tenant=`` query parameter
    (or ``x-tenant-id`` header) selects a tenant-scoped identity; a
    multi-tenant host serves a distinct signed card per tenant.
    """
    return _serve_agent_card(request)


@router.get("/.well-known/agent-card.json", include_in_schema=False)
def agent_card_json(request: Request) -> Response:
    """Serve the signed card at the A2A v1.0 canonical well-known path (#2609).

    A2A v1.0 canonicalised ``/.well-known/agent-card.json``, but at least one
    major shipped client still fetches the legacy ``/.well-known/agent.json``.
    Both paths return identical bytes with the same signature, so a peer
    discovers and verifies the node whichever name it was built to fetch.
    """
    return _serve_agent_card(request)


@router.get("/.well-known/agent.json/keys", include_in_schema=False)
def agent_json_keys(request: Request) -> dict[str, Any]:
    """Return the JWKS for verifying ``/.well-known/agent.json`` signatures.

    JWKS shape per RFC 7517 - ``{"keys": [<jwk>, ...]}``. The current
    orchestrator key always appears first; during a rotation grace window
    (24h by default) any archived public keys still inside the window are
    appended so verifiers cached on the old ``kid`` keep validating until
    their HTTP cache (``Cache-Control: public, max-age=3600`` on the agent
    card route) ages out and they refetch the fresh JWKS. The JWKS is
    tenant-scoped: tenant A's card never verifies against tenant B's keys.
    """
    tenant_id = _resolve_tenant(request)
    # Ensure both the cached PEM and the keystore binding exist before we
    # query the archive - the side-effect of ``_get_signing_keypair`` is
    # what materialises the on-disk directory on first run.
    _private_pem, public_pem = _get_signing_keypair(tenant_id)
    jwks: list[dict[str, str]] = [ed25519_public_jwk(public_pem, kid=_tenant_kid(tenant_id))]
    for archived in _get_keystore(tenant_id).list_archived():
        jwks.append(ed25519_public_jwk(archived.public_pem, kid=archived.kid))
    return {"keys": jwks}


@router.get("/llms.txt", include_in_schema=False, response_class=PlainTextResponse)
def llms_txt() -> str:
    """Return a markdown summary of the public API surface."""
    return _render_llms_txt()


@router.get(HTTP_SIG_DIRECTORY_PATH, include_in_schema=False)
def http_message_signatures_directory() -> dict[str, Any]:
    """Return the key directory (JWKS) for outbound HTTP Message Signatures.

    Verifiers fetch this to validate the RFC 9421 signatures Bernstein places
    on its outbound agent-facing requests (issue #2305). The published keys
    are the install-identity keypair - the exact keystore the outbound signer
    uses - so a signature's ``keyid`` (the install-identity RFC 7638
    thumbprint) always resolves here. When the install identity rotates the
    thumbprint changes, so signatures made under a retired key stop verifying
    against this directory: rotation invalidates old signatures deterministically.
    """
    from bernstein.core.identity import http_signing

    keystore = _get_keystore()
    # Prime the persistent keypair so the directory and the outbound signer
    # converge on the same on-disk key.
    _get_signing_keypair()
    return http_signing.build_key_directory(keystore)
