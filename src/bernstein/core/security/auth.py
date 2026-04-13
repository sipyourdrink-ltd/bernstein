"""SSO / SAML / OIDC authentication for the Bernstein task server.

Provides:
- OIDC and SAML authentication flows for the web dashboard and API
- JWT token issuance and validation for stateless API auth
- Token-based CLI authentication via device authorization flow
- SSO group → Bernstein role mapping (admin / operator / viewer)
- Backwards-compatible legacy bearer token support
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from pathlib import Path

_PERM_BULLETIN_READ = "bulletin:read"

_PERM_COSTS_READ = "costs:read"

_PERM_CLUSTER_READ = "cluster:read"

_PERM_STATUS_READ = "status:read"

_PERM_AGENTS_READ = "agents:read"

_PERM_TASKS_READ = "tasks:read"

_JSON_GLOB = "*.json"

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Roles & permissions
# ---------------------------------------------------------------------------


class AuthRole(StrEnum):
    """Bernstein RBAC roles, ordered by privilege level."""

    ADMIN = "admin"  # Full access: config, users, tasks, agents
    OPERATOR = "operator"  # Task/agent management, no user/config changes
    VIEWER = "viewer"  # Read-only access to dashboard, status, logs


# Permission sets per role
_ROLE_PERMISSIONS: dict[AuthRole, frozenset[str]] = {
    AuthRole.ADMIN: frozenset(
        {
            _PERM_TASKS_READ,
            "tasks:write",
            "tasks:delete",
            _PERM_AGENTS_READ,
            "agents:write",
            "agents:kill",
            _PERM_STATUS_READ,
            _PERM_CLUSTER_READ,
            "cluster:write",
            "auth:manage",
            "config:read",
            "config:write",
            _PERM_COSTS_READ,
            "webhooks:manage",
            _PERM_BULLETIN_READ,
            "bulletin:write",
        }
    ),
    AuthRole.OPERATOR: frozenset(
        {
            _PERM_TASKS_READ,
            "tasks:write",
            _PERM_AGENTS_READ,
            "agents:write",
            "agents:kill",
            _PERM_STATUS_READ,
            _PERM_CLUSTER_READ,
            _PERM_COSTS_READ,
            _PERM_BULLETIN_READ,
            "bulletin:write",
        }
    ),
    AuthRole.VIEWER: frozenset(
        {
            _PERM_TASKS_READ,
            _PERM_AGENTS_READ,
            _PERM_STATUS_READ,
            _PERM_CLUSTER_READ,
            _PERM_COSTS_READ,
            _PERM_BULLETIN_READ,
        }
    ),
}


def role_has_permission(role: AuthRole, permission: str) -> bool:
    """Check if a role has a specific permission."""
    return permission in _ROLE_PERMISSIONS.get(role, frozenset())


# ---------------------------------------------------------------------------
# User & session models
# ---------------------------------------------------------------------------


@dataclass
class AuthUser:
    """An authenticated user identity.

    Created from SSO claims after successful OIDC/SAML authentication.
    Stored in .sdd/auth/users/ as JSON for persistence across restarts.
    """

    id: str  # Unique user ID (from SSO subject claim)
    email: str
    display_name: str
    role: AuthRole = AuthRole.VIEWER
    sso_provider: str = ""  # "oidc" or "saml"
    sso_subject: str = ""  # Subject/NameID from the IdP
    sso_groups: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    last_login_at: float = field(default_factory=time.time)

    def has_permission(self, permission: str) -> bool:
        """Check if this user has a specific permission."""
        return role_has_permission(self.role, permission)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-safe dict."""
        return {
            "id": self.id,
            "email": self.email,
            "display_name": self.display_name,
            "role": self.role.value,
            "sso_provider": self.sso_provider,
            "sso_subject": self.sso_subject,
            "sso_groups": self.sso_groups,
            "created_at": self.created_at,
            "last_login_at": self.last_login_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AuthUser:
        """Deserialize from a dict."""
        return cls(
            id=str(d["id"]),
            email=str(d["email"]),
            display_name=str(d.get("display_name", d.get("email", ""))),
            role=AuthRole(d.get("role", "viewer")),
            sso_provider=str(d.get("sso_provider", "")),
            sso_subject=str(d.get("sso_subject", "")),
            sso_groups=list(d.get("sso_groups", [])),
            created_at=float(d.get("created_at", 0)),
            last_login_at=float(d.get("last_login_at", 0)),
        )


@dataclass
class AuthSession:
    """A user login session.

    Sessions are stored in .sdd/auth/sessions/ for persistence.
    Each session maps to a JWT that was issued to the user.
    """

    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    user_id: str = ""
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0  # Unix timestamp
    revoked: bool = False
    ip_address: str = ""
    user_agent: str = ""

    @property
    def is_expired(self) -> bool:
        return time.time() > self.expires_at

    @property
    def is_valid(self) -> bool:
        return not self.revoked and not self.is_expired

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "revoked": self.revoked,
            "ip_address": self.ip_address,
            "user_agent": self.user_agent,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AuthSession:
        return cls(
            id=str(d["id"]),
            user_id=str(d["user_id"]),
            created_at=float(d.get("created_at", 0)),
            expires_at=float(d.get("expires_at", 0)),
            revoked=bool(d.get("revoked", False)),
            ip_address=str(d.get("ip_address", "")),
            user_agent=str(d.get("user_agent", "")),
        )


@dataclass
class DeviceAuthRequest:
    """A pending CLI device authorization request.

    The CLI initiates a device flow by generating a device code,
    which the user enters in the web dashboard to authorize.
    """

    device_code: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    user_code: str = field(default_factory=lambda: secrets.token_urlsafe(6).upper()[:8])
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    authorized: bool = False
    user_id: str | None = None
    poll_interval_s: int = 5

    def __post_init__(self) -> None:
        if self.expires_at == 0:
            self.expires_at = self.created_at + 600  # 10 minute expiry


# ---------------------------------------------------------------------------
# SSO configuration (Pydantic settings — reads from env / .env)
# ---------------------------------------------------------------------------


class OIDCConfig(BaseSettings):
    """OpenID Connect provider configuration."""

    model_config = SettingsConfigDict(env_prefix="BERNSTEIN_OIDC_", env_file=".env", extra="ignore")

    enabled: bool = False
    issuer_url: str = ""  # e.g. https://accounts.google.com
    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = ""  # e.g. http://localhost:8052/auth/oidc/callback
    scopes: str = "openid email profile groups"  # space-separated
    # Discovery: fetched from {issuer_url}/.well-known/openid-configuration
    authorization_endpoint: str = ""  # Override if not using discovery
    token_endpoint: str = ""  # Override if not using discovery
    userinfo_endpoint: str = ""  # Override if not using discovery
    jwks_uri: str = ""  # Override if not using discovery


class SAMLConfig(BaseSettings):
    """SAML 2.0 Service Provider configuration."""

    model_config = SettingsConfigDict(env_prefix="BERNSTEIN_SAML_", env_file=".env", extra="ignore")

    enabled: bool = False
    idp_entity_id: str = ""
    idp_sso_url: str = ""  # IdP Single Sign-On URL
    idp_slo_url: str = ""  # IdP Single Logout URL (optional)
    idp_x509_cert: str = ""  # IdP certificate (PEM, newlines as \n in env)
    sp_entity_id: str = "bernstein-orchestrator"
    sp_acs_url: str = ""  # e.g. http://localhost:8052/auth/saml/acs
    sp_slo_url: str = ""  # SP Single Logout URL (optional)
    # Attribute mapping: IdP attribute name → Bernstein field
    attr_email: str = "email"
    attr_name: str = "displayName"
    attr_groups: str = "memberOf"


class GroupRoleMappingEntry(BaseSettings):
    """A single SSO group → Bernstein role mapping."""

    model_config = SettingsConfigDict(extra="ignore")

    group: str = ""
    role: str = "viewer"


class SSOConfig(BaseSettings):
    """Top-level SSO/auth configuration."""

    model_config = SettingsConfigDict(env_prefix="BERNSTEIN_AUTH_", env_file=".env", extra="ignore")

    # General
    enabled: bool = False
    jwt_secret: str = Field(default_factory=lambda: secrets.token_urlsafe(32))
    jwt_algorithm: str = "HS256"
    jwt_expiry_seconds: int = 86400  # 24 hours
    session_expiry_seconds: int = 86400  # 24 hours

    # Legacy bearer token (backwards compat)
    legacy_token: str = ""  # Falls back to BERNSTEIN_AUTH_TOKEN env var

    # Default role for authenticated users without a group mapping
    default_role: str = "viewer"

    # Group → role mapping (loaded from .sdd/auth/group_mappings.json)
    # Env vars provide a simple comma-separated list: "admins=admin,devs=operator"
    group_role_map: str = ""  # "group1=admin,group2=operator,group3=viewer"

    # Sub-configs
    oidc: OIDCConfig = Field(default_factory=OIDCConfig)
    saml: SAMLConfig = Field(default_factory=SAMLConfig)


@dataclass(frozen=True)
class ParsedSAMLAssertion:
    """Parsed claims extracted from a SAML assertion."""

    subject: str
    email: str
    display_name: str
    groups: list[str]
    attributes: dict[str, list[str]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# JWT token handling (HMAC-based, no external dependency beyond stdlib)
# ---------------------------------------------------------------------------


def _b64url_encode(data: bytes) -> str:
    """Base64url encode without padding."""
    import base64

    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    """Base64url decode with padding restoration."""
    import base64

    padding = 4 - len(s) % 4
    if padding != 4:
        s += "=" * padding
    return base64.urlsafe_b64decode(s)


def decode_jwt_unverified(token: str) -> dict[str, Any] | None:
    """Decode JWT claims without verifying the signature.

    This is used only for local cache metadata such as expiry timestamps.
    It must not be used for authorization decisions.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        claims: dict[str, Any] = json.loads(_b64url_decode(parts[1]))
    except (json.JSONDecodeError, Exception):
        return None
    return claims


def extract_jwt_expiry(token: str) -> float | None:
    """Return the ``exp`` claim from a JWT without verifying the signature."""
    claims = decode_jwt_unverified(token)
    if claims is None:
        return None
    exp = claims.get("exp")
    if isinstance(exp, int | float):
        return float(exp)
    return None


def create_jwt(
    claims: dict[str, Any],
    secret: str,
    algorithm: str = "HS256",
    expiry_seconds: int = 86400,
) -> str:
    """Create a signed JWT token.

    Uses HMAC-SHA256 by default. No external JWT library required.
    """
    header = {"alg": algorithm, "typ": "JWT"}
    now = int(time.time())
    payload = {
        **claims,
        "iat": now,
        "exp": now + expiry_seconds,
        "jti": uuid.uuid4().hex,
    }

    header_b64 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode())
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{header_b64}.{payload_b64}"

    if algorithm == "HS256":
        sig = hmac.new(secret.encode(), signing_input.encode(), hashlib.sha256).digest()
    elif algorithm == "HS384":
        sig = hmac.new(secret.encode(), signing_input.encode(), hashlib.sha384).digest()
    elif algorithm == "HS512":
        sig = hmac.new(secret.encode(), signing_input.encode(), hashlib.sha512).digest()
    else:
        msg = f"Unsupported algorithm: {algorithm}"
        raise ValueError(msg)

    return f"{signing_input}.{_b64url_encode(sig)}"


def verify_jwt(
    token: str,
    secret: str,
    algorithm: str = "HS256",
) -> dict[str, Any] | None:
    """Verify and decode a JWT token. Returns claims dict or None if invalid."""
    parts = token.split(".")
    if len(parts) != 3:
        return None

    header_b64, payload_b64, sig_b64 = parts

    # Verify header
    try:
        header = json.loads(_b64url_decode(header_b64))
    except (json.JSONDecodeError, Exception):
        return None

    if header.get("alg") != algorithm:
        return None

    # Verify signature
    signing_input = f"{header_b64}.{payload_b64}"
    if algorithm == "HS256":
        expected = hmac.new(secret.encode(), signing_input.encode(), hashlib.sha256).digest()
    elif algorithm == "HS384":
        expected = hmac.new(secret.encode(), signing_input.encode(), hashlib.sha384).digest()
    elif algorithm == "HS512":
        expected = hmac.new(secret.encode(), signing_input.encode(), hashlib.sha512).digest()
    else:
        return None

    try:
        actual = _b64url_decode(sig_b64)
    except Exception:
        return None

    if not hmac.compare_digest(expected, actual):
        return None

    # Decode payload
    try:
        claims: dict[str, Any] = json.loads(_b64url_decode(payload_b64))
    except (json.JSONDecodeError, Exception):
        return None

    # Check expiry
    exp = claims.get("exp", 0)
    if time.time() > exp:
        return None

    return claims


# ---------------------------------------------------------------------------
# Group → Role mapper
# ---------------------------------------------------------------------------


def parse_group_role_map(raw: str) -> dict[str, AuthRole]:
    """Parse a comma-separated group=role mapping string.

    Example: "admins=admin,developers=operator,everyone=viewer"
    """
    mapping: dict[str, AuthRole] = {}
    if not raw.strip():
        return mapping
    for entry in raw.split(","):
        entry = entry.strip()
        if "=" not in entry:
            continue
        group, role_str = entry.split("=", 1)
        group = group.strip()
        role_str = role_str.strip().lower()
        try:
            mapping[group] = AuthRole(role_str)
        except ValueError:
            logger.warning("Invalid role %r in group mapping for group %r", role_str, group)
    return mapping


def resolve_role(
    user_groups: list[str],
    group_role_map: dict[str, AuthRole],
    default_role: AuthRole = AuthRole.VIEWER,
) -> AuthRole:
    """Resolve the highest-privilege role from a user's SSO groups.

    Admin > Operator > Viewer. First match at each level wins.
    """
    _ROLE_PRIORITY = {AuthRole.ADMIN: 0, AuthRole.OPERATOR: 1, AuthRole.VIEWER: 2}
    best_role = default_role
    best_priority = _ROLE_PRIORITY.get(default_role, 2)

    for group in user_groups:
        role = group_role_map.get(group)
        if role is not None:
            priority = _ROLE_PRIORITY.get(role, 2)
            if priority < best_priority:
                best_role = role
                best_priority = priority

    return best_role


# ---------------------------------------------------------------------------
# Auth store — file-based persistence in .sdd/auth/
# ---------------------------------------------------------------------------


class AuthStore:
    """File-based storage for users, sessions, and device auth requests.

    Data is stored in .sdd/auth/ as JSON files:
    - .sdd/auth/users/{user_id}.json
    - .sdd/auth/sessions/{session_id}.json
    - .sdd/auth/devices/{device_code}.json
    - .sdd/auth/group_mappings.json
    """

    def __init__(self, sdd_dir: Path) -> None:
        self._base = sdd_dir / "auth"
        self._users_dir = self._base / "users"
        self._sessions_dir = self._base / "sessions"
        self._devices_dir = self._base / "devices"
        self._ensure_dirs()

    def _ensure_dirs(self) -> None:
        for d in (self._users_dir, self._sessions_dir, self._devices_dir):
            d.mkdir(parents=True, exist_ok=True)

    # -- Users --

    def save_user(self, user: AuthUser) -> None:
        path = self._users_dir / f"{user.id}.json"
        path.write_text(json.dumps(user.to_dict(), indent=2))

    def get_user(self, user_id: str) -> AuthUser | None:
        path = self._users_dir / f"{user_id}.json"
        if not path.exists():
            return None
        try:
            return AuthUser.from_dict(json.loads(path.read_text()))
        except (json.JSONDecodeError, KeyError) as exc:
            logger.warning("Failed to load user %s: %s", user_id, exc)
            return None

    def find_user_by_email(self, email: str) -> AuthUser | None:
        for path in self._users_dir.glob(_JSON_GLOB):
            try:
                data = json.loads(path.read_text())
                if data.get("email") == email:
                    return AuthUser.from_dict(data)
            except (json.JSONDecodeError, KeyError):
                continue
        return None

    def find_user_by_sso_subject(self, provider: str, subject: str) -> AuthUser | None:
        for path in self._users_dir.glob(_JSON_GLOB):
            try:
                data = json.loads(path.read_text())
                if data.get("sso_provider") == provider and data.get("sso_subject") == subject:
                    return AuthUser.from_dict(data)
            except (json.JSONDecodeError, KeyError):
                continue
        return None

    def list_users(self) -> list[AuthUser]:
        users: list[AuthUser] = []
        for path in self._users_dir.glob(_JSON_GLOB):
            try:
                users.append(AuthUser.from_dict(json.loads(path.read_text())))
            except (json.JSONDecodeError, KeyError):
                continue
        return users

    # -- Sessions --

    def save_session(self, session: AuthSession) -> None:
        path = self._sessions_dir / f"{session.id}.json"
        path.write_text(json.dumps(session.to_dict(), indent=2))

    def get_session(self, session_id: str) -> AuthSession | None:
        path = self._sessions_dir / f"{session_id}.json"
        if not path.exists():
            return None
        try:
            return AuthSession.from_dict(json.loads(path.read_text()))
        except (json.JSONDecodeError, KeyError):
            return None

    def revoke_session(self, session_id: str) -> bool:
        session = self.get_session(session_id)
        if session is None:
            return False
        session.revoked = True
        self.save_session(session)
        return True

    def revoke_user_sessions(self, user_id: str) -> int:
        count = 0
        for path in self._sessions_dir.glob(_JSON_GLOB):
            try:
                data = json.loads(path.read_text())
                if data.get("user_id") == user_id and not data.get("revoked", False):
                    data["revoked"] = True
                    path.write_text(json.dumps(data, indent=2))
                    count += 1
            except (json.JSONDecodeError, KeyError):
                continue
        return count

    def cleanup_expired_sessions(self) -> int:
        """Remove expired session files. Returns count removed."""
        now = time.time()
        count = 0
        for path in self._sessions_dir.glob(_JSON_GLOB):
            try:
                data = json.loads(path.read_text())
                if data.get("expires_at", 0) < now:
                    path.unlink()
                    count += 1
            except (json.JSONDecodeError, OSError):
                continue
        return count

    # -- Device auth --

    def save_device_request(self, req: DeviceAuthRequest) -> None:
        path = self._devices_dir / f"{req.device_code}.json"
        path.write_text(
            json.dumps(
                {
                    "device_code": req.device_code,
                    "user_code": req.user_code,
                    "created_at": req.created_at,
                    "expires_at": req.expires_at,
                    "authorized": req.authorized,
                    "user_id": req.user_id,
                    "poll_interval_s": req.poll_interval_s,
                },
                indent=2,
            )
        )

    def get_device_request(self, device_code: str) -> DeviceAuthRequest | None:
        path = self._devices_dir / f"{device_code}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
            return DeviceAuthRequest(
                device_code=data["device_code"],
                user_code=data["user_code"],
                created_at=data["created_at"],
                expires_at=data["expires_at"],
                authorized=data.get("authorized", False),
                user_id=data.get("user_id"),
                poll_interval_s=data.get("poll_interval_s", 5),
            )
        except (json.JSONDecodeError, KeyError):
            return None

    def find_device_by_user_code(self, user_code: str) -> DeviceAuthRequest | None:
        for path in self._devices_dir.glob(_JSON_GLOB):
            try:
                data = json.loads(path.read_text())
                if data.get("user_code") == user_code:
                    return DeviceAuthRequest(
                        device_code=data["device_code"],
                        user_code=data["user_code"],
                        created_at=data["created_at"],
                        expires_at=data["expires_at"],
                        authorized=data.get("authorized", False),
                        user_id=data.get("user_id"),
                        poll_interval_s=data.get("poll_interval_s", 5),
                    )
            except (json.JSONDecodeError, KeyError):
                continue
        return None

    def delete_device_request(self, device_code: str) -> None:
        path = self._devices_dir / f"{device_code}.json"
        if path.exists():
            path.unlink()

    def cleanup_expired_devices(self) -> int:
        now = time.time()
        count = 0
        for path in self._devices_dir.glob(_JSON_GLOB):
            try:
                data = json.loads(path.read_text())
                if data.get("expires_at", 0) < now:
                    path.unlink()
                    count += 1
            except (json.JSONDecodeError, OSError):
                continue
        return count

    # -- Group mappings (from file, supplements env var config) --

    def load_group_mappings(self) -> dict[str, AuthRole]:
        path = self._base / "group_mappings.json"
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text())
            mapping: dict[str, AuthRole] = {}
            for group, role_str in raw.items():
                try:
                    mapping[group] = AuthRole(role_str)
                except ValueError:
                    logger.warning("Invalid role %r for group %r in mappings file", role_str, group)
            return mapping
        except (json.JSONDecodeError, OSError):
            return {}

    def save_group_mappings(self, mappings: dict[str, AuthRole]) -> None:
        path = self._base / "group_mappings.json"
        path.write_text(json.dumps({g: r.value for g, r in mappings.items()}, indent=2))


# ---------------------------------------------------------------------------
# Auth service — orchestrates authentication flows
# ---------------------------------------------------------------------------


class AuthService:
    """Central authentication service.

    Manages OIDC/SAML flows, JWT issuance, session management,
    and user lifecycle.
    """

    def __init__(self, config: SSOConfig, store: AuthStore) -> None:
        self.config = config
        self.store = store
        self._group_role_map: dict[str, AuthRole] = {}
        self._oidc_discovery: dict[str, Any] | None = None
        self._load_group_mappings()

    def _load_group_mappings(self) -> None:
        """Merge env-var and file-based group mappings."""
        self._group_role_map = parse_group_role_map(self.config.group_role_map)
        file_mappings = self.store.load_group_mappings()
        # File mappings take precedence over env var
        self._group_role_map.update(file_mappings)

    @property
    def group_role_map(self) -> dict[str, AuthRole]:
        return dict(self._group_role_map)

    # -- OIDC --

    async def oidc_discover(self) -> dict[str, Any]:
        """Fetch OIDC discovery document (cached)."""
        if self._oidc_discovery is not None:
            return self._oidc_discovery

        import httpx

        oidc = self.config.oidc
        discovery_url = f"{oidc.issuer_url.rstrip('/')}/.well-known/openid-configuration"
        async with httpx.AsyncClient() as client:
            resp = await client.get(discovery_url, timeout=10.0)
            resp.raise_for_status()
            self._oidc_discovery = resp.json()
        return self._oidc_discovery  # type: ignore[return-value]

    def get_oidc_auth_url(self, state: str, discovery: dict[str, Any] | None = None) -> str:
        """Build the OIDC authorization URL for redirect."""
        oidc = self.config.oidc
        auth_endpoint = oidc.authorization_endpoint
        if not auth_endpoint and discovery:
            auth_endpoint = discovery.get("authorization_endpoint", "")

        from urllib.parse import urlencode

        params = {
            "response_type": "code",
            "client_id": oidc.client_id,
            "redirect_uri": oidc.redirect_uri,
            "scope": oidc.scopes,
            "state": state,
        }
        return f"{auth_endpoint}?{urlencode(params)}"

    async def oidc_exchange_code(self, code: str) -> dict[str, Any] | None:
        """Exchange an authorization code for tokens."""
        import httpx

        oidc = self.config.oidc
        discovery = await self.oidc_discover()
        token_endpoint = oidc.token_endpoint or discovery.get("token_endpoint", "")

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                token_endpoint,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": oidc.redirect_uri,
                    "client_id": oidc.client_id,
                    "client_secret": oidc.client_secret,
                },
                timeout=10.0,
            )
            if resp.status_code != 200:
                logger.error("OIDC token exchange failed: %s %s", resp.status_code, resp.text)
                return None
            return resp.json()  # type: ignore[no-any-return]

    async def oidc_get_userinfo(self, access_token: str) -> dict[str, Any] | None:
        """Fetch user info from the OIDC provider."""
        import httpx

        oidc = self.config.oidc
        discovery = await self.oidc_discover()
        userinfo_endpoint = oidc.userinfo_endpoint or discovery.get("userinfo_endpoint", "")

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                userinfo_endpoint,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10.0,
            )
            if resp.status_code != 200:
                logger.error("OIDC userinfo failed: %s", resp.status_code)
                return None
            return resp.json()  # type: ignore[no-any-return]

    async def handle_oidc_callback(
        self, code: str, ip_address: str = "", user_agent: str = ""
    ) -> tuple[AuthUser, str] | None:
        """Complete OIDC flow: exchange code → get userinfo → create/update user → issue JWT.

        Returns (user, jwt_token) or None on failure.
        """
        tokens = await self.oidc_exchange_code(code)
        if not tokens:
            return None

        access_token = tokens.get("access_token", "")
        userinfo = await self.oidc_get_userinfo(access_token)
        if not userinfo:
            return None

        subject = str(userinfo.get("sub", ""))
        email = str(userinfo.get("email", ""))
        name = str(userinfo.get("name", userinfo.get("preferred_username", email)))
        groups: list[str] = userinfo.get("groups", [])
        if isinstance(groups, str):
            groups = [groups]

        user = self._upsert_user(
            provider="oidc",
            subject=subject,
            email=email,
            display_name=name,
            groups=groups,
        )
        token = self._issue_token(user, ip_address=ip_address, user_agent=user_agent)
        return user, token

    # -- SAML --

    def get_saml_auth_redirect_url(self, relay_state: str = "") -> str:
        """Build SAML AuthnRequest redirect URL.

        Generates a minimal SAML 2.0 AuthnRequest using HTTP-Redirect binding.
        """
        import base64
        import zlib
        from urllib.parse import urlencode

        saml = self.config.saml
        request_id = f"_bernstein_{uuid.uuid4().hex}"
        issue_instant = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        authn_request = f"""<samlp:AuthnRequest
    xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol"
    xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion"
    ID="{request_id}"
    Version="2.0"
    IssueInstant="{issue_instant}"
    Destination="{saml.idp_sso_url}"
    AssertionConsumerServiceURL="{saml.sp_acs_url}"
    ProtocolBinding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST">
    <saml:Issuer>{saml.sp_entity_id}</saml:Issuer>
    <samlp:NameIDPolicy Format="urn:oasis:names:tc:SAML:2.0:nameid-format:emailAddress"
        AllowCreate="true"/>
</samlp:AuthnRequest>"""

        # Deflate and base64 encode per SAML HTTP-Redirect binding spec
        compressed = zlib.compress(authn_request.encode())[2:-4]  # raw deflate
        encoded = base64.b64encode(compressed).decode()

        params: dict[str, str] = {"SAMLRequest": encoded}
        if relay_state:
            params["RelayState"] = relay_state
        return f"{saml.idp_sso_url}?{urlencode(params)}"

    def parse_saml_assertion(self, assertion_xml: str) -> ParsedSAMLAssertion | None:
        """Parse a SAML assertion XML payload into normalized Bernstein claims."""
        import xml.etree.ElementTree as ET

        saml = self.config.saml
        try:
            root = ET.fromstring(assertion_xml)
        except ET.ParseError as exc:
            logger.error("Failed to parse SAML assertion XML: %s", exc)
            return None

        ns = {
            "samlp": "urn:oasis:names:tc:SAML:2.0:protocol",
            "saml": "urn:oasis:names:tc:SAML:2.0:assertion",
        }
        status_code = root.find(".//samlp:Status/samlp:StatusCode", ns)
        if status_code is not None:
            status_value = status_code.get("Value", "")
            if "Success" not in status_value:
                logger.error("SAML response status: %s", status_value)
                return None

        name_id_el = root.find(".//saml:Assertion/saml:Subject/saml:NameID", ns)
        subject = name_id_el.text if name_id_el is not None and name_id_el.text else ""

        attributes: dict[str, list[str]] = {}
        for attr_stmt in root.findall(".//saml:Assertion/saml:AttributeStatement/saml:Attribute", ns):
            attr_name = attr_stmt.get("Name", "")
            values = [value.text for value in attr_stmt.findall("saml:AttributeValue", ns) if value.text]
            if attr_name and values:
                attributes[attr_name] = values

        email = (attributes.get(saml.attr_email, [""]) or [""])[0] or subject
        display_name = (attributes.get(saml.attr_name, [""]) or [""])[0] or email
        groups = attributes.get(saml.attr_groups, [])

        if not subject and not email:
            logger.error("SAML response missing both NameID and email attribute")
            return None

        return ParsedSAMLAssertion(
            subject=subject or email,
            email=email,
            display_name=display_name,
            groups=groups,
            attributes=attributes,
        )

    def handle_saml_response(
        self, saml_response_b64: str, ip_address: str = "", user_agent: str = ""
    ) -> tuple[AuthUser, str] | None:
        """Process SAML Response from IdP ACS POST.

        Parses the SAML assertion to extract user attributes.
        Returns (user, jwt_token) or None on failure.

        Note: In production, signature validation against idp_x509_cert
        should be performed. This implementation extracts claims and validates
        basic structure.
        """
        import base64

        try:
            xml_bytes = base64.b64decode(saml_response_b64)
        except Exception as exc:
            logger.error("Failed to parse SAML response: %s", exc)
            return None

        assertion = self.parse_saml_assertion(xml_bytes.decode("utf-8"))
        if assertion is None:
            return None

        user = self._upsert_user(
            provider="saml",
            subject=assertion.subject,
            email=assertion.email,
            display_name=assertion.display_name,
            groups=assertion.groups,
        )
        token = self._issue_token(user, ip_address=ip_address, user_agent=user_agent)
        return user, token

    def get_saml_sp_metadata(self) -> str:
        """Generate SAML SP metadata XML."""
        saml = self.config.saml
        return f"""<?xml version="1.0"?>
<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata"
    entityID="{saml.sp_entity_id}">
    <md:SPSSODescriptor
        protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol"
        AuthnRequestsSigned="false"
        WantAssertionsSigned="true">
        <md:NameIDFormat>urn:oasis:names:tc:SAML:2.0:nameid-format:emailAddress</md:NameIDFormat>
        <md:AssertionConsumerService
            Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST"
            Location="{saml.sp_acs_url}"
            index="0"
            isDefault="true"/>
    </md:SPSSODescriptor>
</md:EntityDescriptor>"""

    # -- User lifecycle --

    def _upsert_user(
        self,
        provider: str,
        subject: str,
        email: str,
        display_name: str,
        groups: list[str],
    ) -> AuthUser:
        """Create or update a user from SSO claims."""
        existing = self.store.find_user_by_sso_subject(provider, subject)
        role = resolve_role(
            groups,
            self._group_role_map,
            AuthRole(self.config.default_role),
        )

        if existing:
            existing.email = email
            existing.display_name = display_name
            existing.role = role
            existing.sso_groups = groups
            existing.last_login_at = time.time()
            self.store.save_user(existing)
            return existing

        user = AuthUser(
            id=uuid.uuid4().hex[:12],
            email=email,
            display_name=display_name,
            role=role,
            sso_provider=provider,
            sso_subject=subject,
            sso_groups=groups,
        )
        self.store.save_user(user)
        return user

    # -- Token issuance --

    def _issue_token(
        self,
        user: AuthUser,
        ip_address: str = "",
        user_agent: str = "",
    ) -> str:
        """Issue a JWT token for a user and create a session."""
        session = AuthSession(
            user_id=user.id,
            expires_at=time.time() + self.config.session_expiry_seconds,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        self.store.save_session(session)

        return create_jwt(
            claims={
                "sub": user.id,
                "email": user.email,
                "role": user.role.value,
                "session_id": session.id,
            },
            secret=self.config.jwt_secret,
            algorithm=self.config.jwt_algorithm,
            expiry_seconds=self.config.jwt_expiry_seconds,
        )

    def validate_token(self, token: str) -> tuple[AuthUser, dict[str, Any]] | None:
        """Validate a JWT token and return (user, claims) or None."""
        claims = verify_jwt(token, self.config.jwt_secret, self.config.jwt_algorithm)
        if not claims:
            return None

        # Verify session is still valid
        session_id = claims.get("session_id", "")
        if session_id:
            session = self.store.get_session(session_id)
            if session and not session.is_valid:
                return None

        user_id = claims.get("sub", "")
        user = self.store.get_user(user_id)
        if not user:
            return None

        return user, claims

    def validate_legacy_token(self, token: str) -> bool:
        """Check if a token matches the legacy bearer token."""
        legacy = self.config.legacy_token
        if not legacy:
            return False
        return hmac.compare_digest(token, legacy)

    # -- Device flow --

    def create_device_request(self) -> DeviceAuthRequest:
        """Create a new device authorization request for CLI auth."""
        req = DeviceAuthRequest()
        self.store.save_device_request(req)
        return req

    def authorize_device(self, user_code: str, user: AuthUser) -> bool:
        """Authorize a device code (called from web dashboard after SSO login)."""
        req = self.store.find_device_by_user_code(user_code)
        if not req or time.time() > req.expires_at:
            return False
        req.authorized = True
        req.user_id = user.id
        self.store.save_device_request(req)
        return True

    def poll_device_token(self, device_code: str) -> tuple[str, str] | None:
        """Poll for device authorization. Returns (jwt_token, "complete") or None.

        Returns:
            ("token", "complete") if authorized
            None if pending, expired, or not found
        """
        req = self.store.get_device_request(device_code)
        if not req:
            return None
        if time.time() > req.expires_at:
            self.store.delete_device_request(device_code)
            return None
        if not req.authorized or not req.user_id:
            return None

        user = self.store.get_user(req.user_id)
        if not user:
            return None

        token = self._issue_token(user)
        self.store.delete_device_request(device_code)
        return token, "complete"

    # -- Logout --

    def logout(self, session_id: str) -> bool:
        """Revoke a session."""
        return self.store.revoke_session(session_id)
