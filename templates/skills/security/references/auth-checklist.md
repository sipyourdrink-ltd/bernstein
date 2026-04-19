# Authentication review checklist

## Tokens
- JWT: reject `alg=none`, verify signature, enforce `exp` and `nbf`.
- Refresh tokens rotate on use; revoke on logout and on detected reuse.
- Bearer tokens never appear in query strings or referrers.

## Sessions
- Cookies: `Secure`, `HttpOnly`, `SameSite=Lax` minimum.
- Idle timeout + absolute timeout, both enforced server-side.
- Session IDs re-generated on privilege change.

## OAuth / OIDC
- Verify state and nonce.
- Validate `iss`, `aud`, `exp`, `sub`.
- Enforce PKCE for public clients.

## SAML
- Validate assertion signature with the IdP's cert.
- Parse with a hardened XML parser (`defusedxml`).
- Check `NotBefore` / `NotOnOrAfter` and single-use semantics.

## Credentials
- Bcrypt / Argon2 with a per-user salt.
- Never log raw passwords or hashed passwords.
- Rate-limit login and password-reset endpoints.
