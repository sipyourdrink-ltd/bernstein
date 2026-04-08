# SSO/OIDC Enterprise IdP Integration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden the existing OIDC authentication flow to work reliably with enterprise IdPs (Okta, Azure AD, Google Workspace) by adding PKCE to the OIDC authorization code flow and supporting IdP-specific group claim extraction for RBAC role mapping.

**Architecture:** `auth.py`'s `AuthService` already implements OIDC discovery, code exchange, userinfo, and group→role mapping. `sso_oidc.py` is a standalone duplicate. This plan adds PKCE to `AuthService`'s OIDC flow (required by Okta/Azure AD public clients), adds configurable group claim names for IdP-specific extraction, and writes an integration test suite. `sso_oidc.py` is left as-is (not integrated, not deleted — no cleanup churn).

**Tech Stack:** Python 3.12+, FastAPI, httpx, existing `AuthService`, `oauth_pkce.py` PKCE utilities

---

## Discovery Audit — Current State

### What EXISTS in `auth.py` AuthService (lines 727-862):
- `oidc_discover()` — fetches `.well-known/openid-configuration` with caching ✓
- `get_oidc_auth_url(state, discovery)` — builds authorization URL ✓
- `oidc_exchange_code(code)` — exchanges auth code for tokens ✓
- `oidc_get_userinfo(access_token)` — fetches userinfo endpoint ✓
- `handle_oidc_callback(code, ip, user_agent)` — full flow: exchange → userinfo → upsert user → issue JWT ✓
- `_upsert_user(provider, subject, email, display_name, groups)` — creates/updates user with role resolution ✓
- `resolve_role(user_groups, group_role_map)` — maps groups to highest-privilege AuthRole ✓

### What EXISTS in `routes/auth.py` (520 lines):
- `GET /auth/providers` — lists enabled auth methods ✓
- `GET /auth/login?provider=oidc` — initiates OIDC redirect ✓
- `GET /auth/oidc/callback` — handles OIDC callback ✓
- `GET /auth/me` — current user profile ✓
- `POST /auth/logout` — session revocation ✓
- Group mapping CRUD endpoints ✓

### What EXISTS in `oauth_pkce.py` (344 lines):
- `generate_code_verifier()` — 128-char cryptographic verifier ✓
- `generate_code_challenge()` — S256 challenge derivation ✓
- `generate_pkce_pair()` — returns (verifier, challenge) tuple ✓

### What is MISSING:
1. **PKCE in OIDC flow** — `auth.py`'s `get_oidc_auth_url()` and `oidc_exchange_code()` don't include `code_verifier`/`code_challenge` parameters. Okta and Azure AD public clients require PKCE (RFC 7636).
2. **Configurable group claim** — `handle_oidc_callback()` hard-codes `userinfo.get("groups", [])`. Azure AD uses `roles` or custom claims. Google Workspace uses `hd` (hosted domain) for org verification, not group claims.
3. **IdP group claim fallback chain** — No support for checking multiple claim names (e.g., try `groups`, then `roles`, then `memberOf`).

### What is OUT OF SCOPE:
- Deleting or refactoring `sso_oidc.py` (separate cleanup task)
- SAML changes (already implemented separately)
- UI/dashboard changes
- JWT signature verification with JWKS (would require `PyJWT` or manual RS256 — separate security task)

---

## File Structure

| Action | File | Responsibility |
|--------|------|---------------|
| Modify | `src/bernstein/core/auth.py:240-242,769-785,787-810,831-862` | Add PKCE params, configurable group claims |
| Create | `tests/unit/test_oidc_enterprise.py` | Tests for PKCE in OIDC flow and multi-claim group extraction |

---

### Task 1: Add PKCE Support to OIDC Authorization Flow

**Files:**
- Modify: `src/bernstein/core/auth.py:240-242,769-810`
- Test: `tests/unit/test_oidc_enterprise.py`

- [ ] **Step 1: Write the failing test for PKCE in OIDC auth URL**

```python
"""Tests for enterprise IdP OIDC integration (PKCE + group claims)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import pytest

from bernstein.core.auth import AuthService, AuthStore, OIDCConfig, SSOConfig


def _make_sso_config(**oidc_overrides: object) -> SSOConfig:
    """Create an SSOConfig with OIDC enabled."""
    oidc_defaults = {
        "enabled": True,
        "issuer_url": "https://idp.example.com",
        "client_id": "bernstein-test",
        "client_secret": "test-secret",
        "redirect_uri": "http://localhost:8052/auth/oidc/callback",
        "authorization_endpoint": "https://idp.example.com/authorize",
        "token_endpoint": "https://idp.example.com/token",
        "userinfo_endpoint": "https://idp.example.com/userinfo",
    }
    oidc_defaults.update(oidc_overrides)
    oidc = OIDCConfig(**oidc_defaults)
    return SSOConfig(oidc=oidc, jwt_secret="test-secret-key-minimum-32-chars!!")


class TestOIDCPKCE:
    """Test PKCE support in the OIDC authorization flow."""

    def test_auth_url_includes_pkce_challenge(self, tmp_path: object) -> None:
        """Authorization URL must include code_challenge and code_challenge_method."""
        store = AuthStore(base_dir=tmp_path)  # type: ignore[arg-type]
        service = AuthService(config=_make_sso_config(), store=store)
        discovery = {
            "authorization_endpoint": "https://idp.example.com/authorize",
            "token_endpoint": "https://idp.example.com/token",
        }
        url, _verifier = service.get_oidc_auth_url_with_pkce(state="test-state", discovery=discovery)
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        assert "code_challenge" in params
        assert params["code_challenge_method"] == ["S256"]

    def test_auth_url_returns_verifier(self, tmp_path: object) -> None:
        """get_oidc_auth_url_with_pkce must return the code_verifier for later exchange."""
        store = AuthStore(base_dir=tmp_path)  # type: ignore[arg-type]
        service = AuthService(config=_make_sso_config(), store=store)
        discovery = {
            "authorization_endpoint": "https://idp.example.com/authorize",
            "token_endpoint": "https://idp.example.com/token",
        }
        _url, verifier = service.get_oidc_auth_url_with_pkce(state="test-state", discovery=discovery)
        assert len(verifier) >= 43  # RFC 7636 minimum
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_oidc_enterprise.py::TestOIDCPKCE::test_auth_url_includes_pkce_challenge -x -q`
Expected: FAIL — `AttributeError: 'AuthService' object has no attribute 'get_oidc_auth_url_with_pkce'`

- [ ] **Step 3: Add `get_oidc_auth_url_with_pkce()` to AuthService**

In `src/bernstein/core/auth.py`, add this method to `AuthService` after the existing `get_oidc_auth_url` method (around line 785):

```python
def get_oidc_auth_url_with_pkce(
    self, state: str, discovery: dict[str, Any] | None = None
) -> tuple[str, str]:
    """Build OIDC authorization URL with PKCE (RFC 7636).

    Args:
        state: CSRF state parameter.
        discovery: Optional pre-fetched discovery document.

    Returns:
        Tuple of (authorization_url, code_verifier).
    """
    from bernstein.core.oauth_pkce import generate_pkce_pair

    verifier, challenge = generate_pkce_pair()
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
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{auth_endpoint}?{urlencode(params)}", verifier
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_oidc_enterprise.py::TestOIDCPKCE -x -q`
Expected: PASS

- [ ] **Step 5: Add PKCE verifier to token exchange**

Add `oidc_exchange_code_with_pkce()` to AuthService after `oidc_exchange_code` (around line 810):

```python
async def oidc_exchange_code_with_pkce(
    self, code: str, code_verifier: str
) -> dict[str, Any] | None:
    """Exchange authorization code for tokens with PKCE verifier.

    Args:
        code: Authorization code from callback.
        code_verifier: PKCE code verifier from auth URL generation.

    Returns:
        Token response dict, or None on failure.
    """
    import httpx

    oidc = self.config.oidc
    discovery = await self.oidc_discover()
    token_endpoint = oidc.token_endpoint or discovery.get("token_endpoint", "")

    data: dict[str, str] = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": oidc.redirect_uri,
        "client_id": oidc.client_id,
        "code_verifier": code_verifier,
    }
    if oidc.client_secret:
        data["client_secret"] = oidc.client_secret

    async with httpx.AsyncClient() as client:
        resp = await client.post(token_endpoint, data=data, timeout=10.0)
        if resp.status_code != 200:
            logger.error("OIDC PKCE token exchange failed: %s %s", resp.status_code, resp.text)
            return None
        return resp.json()  # type: ignore[no-any-return]
```

- [ ] **Step 6: Write test for PKCE token exchange**

Add to `tests/unit/test_oidc_enterprise.py`:

```python
class TestOIDCPKCEExchange:
    @pytest.mark.asyncio()
    async def test_exchange_includes_code_verifier(self, tmp_path: object) -> None:
        """Token exchange request must include code_verifier."""
        store = AuthStore(base_dir=tmp_path)  # type: ignore[arg-type]
        config = _make_sso_config()
        service = AuthService(config=config, store=store)
        # Pre-load discovery cache
        service._oidc_discovery = {
            "token_endpoint": "https://idp.example.com/token",
            "userinfo_endpoint": "https://idp.example.com/userinfo",
        }

        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access_token": "at_test",
            "id_token": "idt_test",
            "token_type": "Bearer",
            "expires_in": 3600,
        }

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            result = await service.oidc_exchange_code_with_pkce("auth-code", "test-verifier")

            assert result is not None
            assert result["access_token"] == "at_test"
            # Verify code_verifier was included in the POST
            call_kwargs = mock_client.post.call_args
            assert call_kwargs.kwargs["data"]["code_verifier"] == "test-verifier"
```

- [ ] **Step 7: Run all PKCE tests**

Run: `uv run pytest tests/unit/test_oidc_enterprise.py -x -q`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add src/bernstein/core/auth.py tests/unit/test_oidc_enterprise.py
git commit -m "feat(auth): add PKCE support to OIDC authorization flow for enterprise IdPs"
```

---

### Task 2: Add Configurable Group Claim Extraction

**Files:**
- Modify: `src/bernstein/core/auth.py:240-242,848-852`
- Test: `tests/unit/test_oidc_enterprise.py`

- [ ] **Step 1: Write the failing test for configurable group claims**

Add to `tests/unit/test_oidc_enterprise.py`:

```python
class TestGroupClaimExtraction:
    """Test IdP-specific group claim extraction."""

    def test_default_group_claim_is_groups(self) -> None:
        config = OIDCConfig(
            enabled=True,
            issuer_url="https://idp.example.com",
            client_id="test",
        )
        assert config.group_claims == ("groups",)

    def test_custom_group_claims(self) -> None:
        """Azure AD uses 'roles', some IdPs use 'memberOf'."""
        config = OIDCConfig(
            enabled=True,
            issuer_url="https://login.microsoftonline.com/tenant",
            client_id="test",
            group_claims="roles,groups,memberOf",
        )
        assert config.group_claims == ("roles", "groups", "memberOf")

    def test_extract_groups_from_primary_claim(self, tmp_path: object) -> None:
        store = AuthStore(base_dir=tmp_path)  # type: ignore[arg-type]
        service = AuthService(
            config=_make_sso_config(group_claims="roles,groups"),
            store=store,
        )
        userinfo = {"sub": "u1", "email": "a@b.com", "roles": ["admin", "dev"]}
        groups = service._extract_groups(userinfo)
        assert groups == ["admin", "dev"]

    def test_extract_groups_fallback_to_secondary_claim(self, tmp_path: object) -> None:
        store = AuthStore(base_dir=tmp_path)  # type: ignore[arg-type]
        service = AuthService(
            config=_make_sso_config(group_claims="roles,groups"),
            store=store,
        )
        userinfo = {"sub": "u1", "email": "a@b.com", "groups": ["eng-team"]}
        groups = service._extract_groups(userinfo)
        assert groups == ["eng-team"]

    def test_extract_groups_string_value(self, tmp_path: object) -> None:
        """Some IdPs return a single string instead of a list."""
        store = AuthStore(base_dir=tmp_path)  # type: ignore[arg-type]
        service = AuthService(
            config=_make_sso_config(group_claims="groups"),
            store=store,
        )
        userinfo = {"sub": "u1", "email": "a@b.com", "groups": "single-group"}
        groups = service._extract_groups(userinfo)
        assert groups == ["single-group"]

    def test_extract_groups_no_matching_claim(self, tmp_path: object) -> None:
        store = AuthStore(base_dir=tmp_path)  # type: ignore[arg-type]
        service = AuthService(
            config=_make_sso_config(group_claims="roles,groups"),
            store=store,
        )
        userinfo = {"sub": "u1", "email": "a@b.com"}
        groups = service._extract_groups(userinfo)
        assert groups == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_oidc_enterprise.py::TestGroupClaimExtraction::test_default_group_claim_is_groups -x -q`
Expected: FAIL — `AttributeError: 'OIDCConfig' object has no attribute 'group_claims'`

- [ ] **Step 3: Add `group_claims` field to OIDCConfig**

In `src/bernstein/core/auth.py`, find the `OIDCConfig` class (around line 230) and add the `group_claims` field. This is a comma-separated string that gets parsed into a tuple:

```python
group_claims: str = "groups"  # comma-separated claim names to check for group memberships
```

Add a property to parse it:

```python
@property
def group_claims_tuple(self) -> tuple[str, ...]:
    """Parse group_claims into a tuple of claim names."""
    return tuple(c.strip() for c in self.group_claims.split(",") if c.strip())
```

Note: Since `OIDCConfig` uses `BaseSettings` (pydantic), check if it supports properties. If not, use a computed field or a standalone function.

- [ ] **Step 4: Add `_extract_groups()` method to AuthService**

Add to the `AuthService` class (after `_load_group_mappings`, around line 747):

```python
def _extract_groups(self, userinfo: dict[str, Any]) -> list[str]:
    """Extract group memberships from userinfo using configurable claim names.

    Checks each claim name in order; uses the first one that has data.
    Handles both list and string values.

    Args:
        userinfo: Userinfo response dict from the IdP.

    Returns:
        List of group membership strings.
    """
    claims = tuple(
        c.strip() for c in self.config.oidc.group_claims.split(",") if c.strip()
    )
    for claim in claims:
        value = userinfo.get(claim)
        if value is not None:
            if isinstance(value, str):
                return [value]
            if isinstance(value, list):
                return [str(v) for v in value]
    return []
```

- [ ] **Step 5: Update `handle_oidc_callback` to use `_extract_groups()`**

In `handle_oidc_callback` (around line 850), replace:

```python
groups: list[str] = userinfo.get("groups", [])
if isinstance(groups, str):
    groups = [groups]
```

with:

```python
groups = self._extract_groups(userinfo)
```

- [ ] **Step 6: Run all group claim tests**

Run: `uv run pytest tests/unit/test_oidc_enterprise.py::TestGroupClaimExtraction -x -q`
Expected: PASS

- [ ] **Step 7: Run existing auth tests for regression**

Run: `uv run pytest tests/unit/test_auth.py -x -q`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add src/bernstein/core/auth.py tests/unit/test_oidc_enterprise.py
git commit -m "feat(auth): configurable group claim extraction for enterprise IdPs (Azure AD, Okta, Google)"
```

---

## Workflow Spec: OIDC Authentication with PKCE

```
TRIGGER: User clicks "Login with SSO" or GET /auth/login?provider=oidc

STEP 1: Generate PKCE pair + state
  Actor: AuthService.get_oidc_auth_url_with_pkce()
  Action: Generate (code_verifier, code_challenge) via S256, generate CSRF state
  Output: (authorization_url, code_verifier)
  Store: code_verifier in server-side session/state store keyed by state param

STEP 2: Redirect to IdP
  Actor: routes/auth.py login endpoint
  Action: HTTP 302 redirect to authorization_url
  Customer sees: IdP login page (Okta/Azure AD/Google)

STEP 3: IdP authenticates user
  Actor: External IdP
  Action: User enters credentials, IdP validates, IdP redirects back
  Output: GET /auth/oidc/callback?code=AUTH_CODE&state=STATE
  FAILURE(user_denied): IdP redirects with ?error=access_denied → show error

STEP 4: Exchange code for tokens (with PKCE verifier)
  Actor: AuthService.oidc_exchange_code_with_pkce(code, verifier)
  Action: POST to IdP token endpoint with code + code_verifier
  Timeout: 10s
  Output on SUCCESS: {access_token, id_token, refresh_token, expires_in}
  Output on FAILURE:
    - FAILURE(invalid_grant): Code expired or verifier mismatch → show error, no cleanup
    - FAILURE(timeout): IdP unreachable → show error "SSO provider unavailable"

STEP 5: Fetch userinfo
  Actor: AuthService.oidc_get_userinfo(access_token)
  Action: GET IdP userinfo endpoint
  Timeout: 10s
  Output: {sub, email, name, groups/roles/memberOf}
  FAILURE: → show error "Could not retrieve user info"

STEP 6: Extract groups from configurable claims
  Actor: AuthService._extract_groups(userinfo)
  Action: Check claim names in order (e.g., "roles", "groups", "memberOf")
  Output: list[str] of group memberships (may be empty)

STEP 7: Resolve role from groups
  Actor: resolve_role(groups, group_role_map)
  Action: Map first matching group to AuthRole, fallback to VIEWER
  Output: AuthRole (ADMIN | OPERATOR | VIEWER)

STEP 8: Upsert user + issue JWT
  Actor: AuthService._upsert_user() + _issue_token()
  Action: Create or update AuthUser in .sdd/auth/, create AuthSession, sign JWT
  Output: (AuthUser, jwt_token)
  Customer sees: Redirect to dashboard with session cookie

STATE TRANSITIONS:
  [unauthenticated] → (OIDC callback success) → [authenticated]
  [unauthenticated] → (OIDC callback failure) → [unauthenticated + error]
  [authenticated] → (session expires) → [unauthenticated]
  [authenticated] → (POST /auth/logout) → [unauthenticated]
```

---

## Assumptions

| # | Assumption | Verified | Risk if wrong |
|---|-----------|----------|---------------|
| A1 | `OIDCConfig` is a pydantic `BaseSettings` subclass that accepts new string fields | Verified in auth.py:225 | Field addition fails at import |
| A2 | `oauth_pkce.generate_pkce_pair()` returns `(verifier: str, challenge: str)` | Verified in oauth_pkce.py | Wrong unpacking order |
| A3 | State/verifier storage between auth URL generation and callback is handled by the route layer (not AuthService) | Needs verification in routes/auth.py | Verifier lost between requests |
| A4 | Enterprise IdPs return groups in the userinfo response (not only in the ID token JWT claims) | Depends on IdP config | Groups extraction returns empty list; fallback to viewer role |
