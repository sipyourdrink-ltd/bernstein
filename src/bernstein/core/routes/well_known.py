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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse, Response

from bernstein import __version__ as _BERNSTEIN_VERSION
from bernstein.core.security.agent_card_keystore import (
    DEFAULT_KEY_DIR,
    AgentCardKeystore,
)
from bernstein.core.security.agent_card_signer import (
    canonicalize_jcs,
    ed25519_public_jwk,
)

router = APIRouter()

#: Well-known path publishing the key directory (JWKS) that verifiers fetch to
#: validate outbound HTTP Message Signatures (issue #2305). The path follows
#: the Web Bot Auth convention so third-party sites can locate it. The keys
#: served here are the install-identity keypair - the same keystore used to
#: sign outbound agent-facing requests - each keyed by its RFC 7638 thumbprint.
HTTP_SIG_DIRECTORY_PATH = "/.well-known/http-message-signatures-directory"

_AGENT_NAME = "bernstein"
_AGENT_DESCRIPTION = (
    "Bernstein orchestrates short-lived CLI coding agents (Claude Code, "
    "Codex, Gemini CLI, Aider, ...) against a file-based task store.  "
    "Clients submit tasks, query status, and post cross-agent bulletins "
    "via the documented endpoints below."
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
# bind ``_KEYSTORE`` -- a plain ``threading.Lock`` self-deadlocks on the very
# first JWKS call. ``rotate_agent_card_keys()`` has the same nesting shape.
_KEY_LOCK = threading.RLock()
_KEYSTORE: AgentCardKeystore | None = None
_PRIVATE_PEM: bytes | None = None
_PUBLIC_PEM: bytes | None = None


def _resolve_key_dir() -> Path:
    """Return the directory backing the persistent keystore.

    Honours ``BERNSTEIN_AGENT_CARD_KEY_DIR`` so production deployments can
    point at a mounted secret volume; falls back to ``.bernstein/keys`` in
    the working directory.
    """
    override = os.environ.get("BERNSTEIN_AGENT_CARD_KEY_DIR", "").strip()
    if override:
        return Path(override)
    return DEFAULT_KEY_DIR


def _get_keystore() -> AgentCardKeystore:
    """Return the process-wide :class:`AgentCardKeystore`, creating it lazily."""
    global _KEYSTORE
    if _KEYSTORE is not None:
        return _KEYSTORE
    with _KEY_LOCK:
        if _KEYSTORE is None:
            _KEYSTORE = AgentCardKeystore(_resolve_key_dir())
    return _KEYSTORE


def _get_signing_keypair() -> tuple[bytes, bytes]:
    """Return the cached signing keypair, loading from disk on first use."""
    global _PRIVATE_PEM, _PUBLIC_PEM
    if _PRIVATE_PEM is not None and _PUBLIC_PEM is not None:
        return _PRIVATE_PEM, _PUBLIC_PEM
    with _KEY_LOCK:
        if _PRIVATE_PEM is None or _PUBLIC_PEM is None:
            _PRIVATE_PEM, _PUBLIC_PEM = _get_keystore().load_or_generate()
    return _PRIVATE_PEM, _PUBLIC_PEM


def _reset_signing_keypair_for_tests(key_dir: Path | None = None) -> None:
    """Reset both the keystore binding and the cached PEM bytes.

    Tests pass ``key_dir=tmp_path / "keys"`` so each case gets a fresh
    directory; production callers leave ``key_dir=None`` (the default
    persistent directory will be re-bound on next request).
    """
    global _KEYSTORE, _PRIVATE_PEM, _PUBLIC_PEM
    with _KEY_LOCK:
        _KEYSTORE = AgentCardKeystore(key_dir) if key_dir is not None else None
        _PRIVATE_PEM = None
        _PUBLIC_PEM = None


def rotate_agent_card_keys() -> tuple[bytes, bytes]:
    """Rotate the persistent agent-card keypair.

    Archives the current keypair under ``<key_dir>/archive/<isoformat>/`` and
    mints a fresh one with ``O_EXCL`` + ``0o600`` semantics. The JWKS
    endpoint will continue to publish the rotated-out public key for the
    keystore's grace window (24h by default) so verifiers cached on the
    old ``kid`` keep validating until their HTTP cache ages out.

    Returns:
        The freshly-generated ``(private_pem, public_pem)`` so callers can
        log the new ``kid`` or trigger downstream secret-store sync.
    """
    global _PRIVATE_PEM, _PUBLIC_PEM
    with _KEY_LOCK:
        priv, pub = _get_keystore().rotate()
        _PRIVATE_PEM, _PUBLIC_PEM = priv, pub
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


def _agent_card_body(base_url: str = _DEFAULT_BASE_URL) -> dict[str, Any]:
    """Build the A2A v1.0 card body - the bytes the JWS attests to.

    The result excludes the ``signatures`` array; ``_agent_card_payload``
    appends the JWS list after JCS-canonicalising this body.

    Args:
        base_url: Public base URL of the task server.

    Returns:
        JSON-serialisable dict with the v1.0-mandated fields.
    """
    return {
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


def _resolve_base_url() -> str:
    """Return the base URL to advertise in the card.

    Priority: ``BERNSTEIN_PUBLIC_BASE_URL`` env override → default. Keeping
    this configurable lets reverse-proxied deployments expose the correct
    canonical URL without hardcoding it at build time.
    """
    return os.environ.get("BERNSTEIN_PUBLIC_BASE_URL", _DEFAULT_BASE_URL)


def _agent_card_payload(base_url: str = _DEFAULT_BASE_URL) -> dict[str, Any]:
    """Build the full A2A v1.0 card payload - body plus signatures.

    Verifiers strip ``signatures`` from this payload, JCS-canonicalise the
    rest, and compare against the ``signatures[].jws`` header+sig segments
    using the public key fetched from ``/.well-known/agent.json/keys``.

    Args:
        base_url: Public base URL of the task server.

    Returns:
        Full v1.0 payload dict ready to JSON-serialise.
    """
    body = _agent_card_body(base_url)
    canonical = canonicalize_jcs(body)
    private_pem, _public_pem = _get_signing_keypair()
    jws = _sign_canonical_body(canonical, private_pem, kid=_DEFAULT_KID)
    body["signatures"] = [
        {
            "kid": _DEFAULT_KID,
            "alg": "EdDSA",
            "typ": "agent-card+jws",
            "jws": jws,
        }
    ]
    return body


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


@router.get("/.well-known/agent.json", include_in_schema=False)
def agent_json() -> Response:
    """Return the A2A v1.0 signed agent card for this task server.

    Body bytes are JCS-canonical (RFC 8785) so verifiers can recompute the
    JWS signing input bit-perfect after stripping the ``signatures`` array.
    Cache for an hour - the card body changes only when the server config
    or the orchestrator's signing key rotates.
    """
    payload = _agent_card_payload(_resolve_base_url())
    body = canonicalize_jcs(payload)
    return Response(
        content=body,
        media_type="application/json",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/.well-known/agent.json/keys", include_in_schema=False)
def agent_json_keys() -> dict[str, Any]:
    """Return the JWKS for verifying ``/.well-known/agent.json`` signatures.

    JWKS shape per RFC 7517 - ``{"keys": [<jwk>, ...]}``. The current
    orchestrator key always appears first; during a rotation grace window
    (24h by default) any archived public keys still inside the window are
    appended so verifiers cached on the old ``kid`` keep validating until
    their HTTP cache (``Cache-Control: public, max-age=3600`` on the agent
    card route) ages out and they refetch the fresh JWKS.
    """
    # Ensure both the cached PEM and the keystore binding exist before we
    # query the archive - the side-effect of ``_get_signing_keypair`` is
    # what materialises the on-disk directory on first run.
    _private_pem, public_pem = _get_signing_keypair()
    jwks: list[dict[str, str]] = [ed25519_public_jwk(public_pem, kid=_DEFAULT_KID)]
    for archived in _get_keystore().list_archived():
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
