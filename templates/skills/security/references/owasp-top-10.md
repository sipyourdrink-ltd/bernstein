# OWASP Top 10 audit checklist

Walk through every item when reviewing new code paths. Each finding gets
a severity tag and a concrete fix recommendation.

1. **Broken Access Control** — object-level authorization, path traversal,
   force-browsing, IDOR.
2. **Cryptographic Failures** — weak ciphers, missing TLS, hardcoded keys,
   predictable randomness.
3. **Injection** — SQL, NoSQL, LDAP, OS command, prompt injection.
4. **Insecure Design** — threat model, abuse cases, rate-limiting gaps.
5. **Security Misconfiguration** — default creds, verbose errors, open CORS,
   disabled security headers.
6. **Vulnerable Components** — outdated deps, unreviewed transitive packages.
7. **Authentication Failures** — credential stuffing, missing MFA, weak
   session handling, JWT `alg=none`.
8. **Software and Data Integrity Failures** — unsigned releases, supply
   chain (package substitution, dependency confusion).
9. **Logging and Monitoring Failures** — missing audit trail, PII in logs.
10. **Server-Side Request Forgery** — unrestricted fetches to internal
    hosts, metadata endpoints.

## Severity guide
- **Critical** — remote code execution, auth bypass, mass data leak.
- **High** — targeted data leak, privilege escalation with interaction.
- **Medium** — information disclosure without credentials involved.
- **Low** — defence-in-depth issues, hardening opportunities.
- **Informational** — best-practice suggestions.
