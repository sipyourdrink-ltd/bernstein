# N52 — SSO via OIDC

**Priority:** P3
**Scope:** small (10-20 min)
**Wave:** 3 — Enterprise Readiness

## Problem
Bernstein Cloud / web dashboard has no single sign-on support, forcing enterprise teams to manage separate credentials instead of using their existing identity provider.

## Solution
- Integrate OIDC authentication using the `authlib` library
- Support Okta, Azure AD, and Google as identity providers
- Store authenticated session in a signed JWT cookie
- Add OIDC configuration section to bernstein.yaml (issuer URL, client ID, client secret, redirect URI)
- Implement login callback endpoint and token validation middleware

## Acceptance
- [ ] `authlib` is added as a dependency
- [ ] OIDC login flow works end-to-end with Okta, Azure AD, and Google
- [ ] Authenticated session is stored in a signed JWT cookie
- [ ] Invalid or expired sessions redirect to login
- [ ] OIDC provider settings are configurable in bernstein.yaml
