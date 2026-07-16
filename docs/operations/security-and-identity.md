# Security and Identity

Audience: security engineers evaluating Bernstein for an enterprise
deployment.

## Overview

Bernstein's security model has two axes. The **human axis** authenticates
operators against an identity provider (OIDC, SAML, or local username +
password) and enforces RBAC on the FastAPI surface; tokens are JWT, sessions
are persisted to `.sdd/auth/`, and every privileged action is recorded in an
HMAC-chained tamper-evident audit log. The **agent axis** treats every spawned
agent as a first-class identity: the orchestrator issues a per-agent JWT
scoped to specific tasks and permissions, the API middleware validates that
scope on every mutating request, and revocation is one POST.

Authentication is required by default (`auth_middleware.py:14-19`). The only
path to "no auth" is an explicit opt-out (`BERNSTEIN_AUTH_DISABLED=1` or
`auth.enabled: false` in `bernstein.yaml`), which logs a loud warning on
startup. SSO providers, RBAC route mapping, identity issuance, audit
integrity, drain/export endpoints, and SBOM generation are all in-tree
features - there is no separate "enterprise edition" toggle.

## Auth providers

Three provider families are supported, configured under `auth.*` in
`bernstein.yaml` and exposed by `core/security/auth.py:923-...`
(`AuthService`).

| Provider         | Config keys                                              | Code                                                      |
| ---------------- | -------------------------------------------------------- | --------------------------------------------------------- |
| **OIDC**         | `auth.oidc.{enabled,issuer,client_id,client_secret,scopes,redirect_uri}` | `core/security/sso_oidc.py`, `routes/auth.py:172-261`     |
| **SAML 2.0**     | `auth.saml.{enabled,sp_entity_id,idp_metadata_url,...}`  | `core/security/auth.py` (SAML helpers), `routes/auth.py:269-316` |
| **Local users**  | `auth.users[]` (admin-managed via `/auth/users`)         | `core/security/auth.py` (`AuthUserStore`), `routes/auth.py:494-520` |

Group-to-role mappings are surfaced via `GET /auth/group-mappings` and
modified by admins via `PUT /auth/group-mappings`
(`routes/auth.py:443-491`). They map IdP group claims (e.g.
`bernstein-admins`) to one of the three Bernstein roles.

A fourth path - **legacy bearer tokens** - exists for backwards
compatibility (`auth_middleware.py:7`). It accepts a single shared secret
configured by `BERNSTEIN_AUTH_TOKEN`. Treat it as a transitional
mechanism; SSO + JWT is the supported deployment.

The `/auth/providers` endpoint returns which providers are enabled
(`routes/auth.py:147-164`); use this to drive a self-describing login UI.

## JWT lifecycle

Token implementation: `core/security/jwt_tokens.py:31-93` (`JWTManager`).
Default algorithm: `HS256` with a 24-hour expiry; both knobs live on
`JWTConfig` and are owned by the operator.

**Issuance.** Tokens are minted by `JWTManager.create_token(session_id,
user_id, scopes)`. Three issuers exist:

- **Operator login** - OIDC/SAML callback (`routes/auth.py:212-261`,
  `:269-308`) returns an HTML page that stores the token in
  `localStorage`. Device flow (`/auth/cli/device`, `/auth/cli/token`)
  issues the same token via polling for CLI-based logins
  (`routes/auth.py:324-372`).
- **Agent identity** - `core/agent_identity.py` issues task-scoped JWTs
  with claims `{session_id, user_id=identity_id, task_ids: [...],
  permissions: [...]}`. Stored in `.sdd/auth/identities/`.
- **Cluster nodes** - `ClusterAuthenticator.issue_node_token(node_id)`
  (`core/protocols/cluster/cluster_auth.py:70-93`); see
  [Cluster mode](cluster-mode.md).

**Refresh.** `POST /auth/refresh` (mounted alongside `/auth/token` per
A2's endpoint inventory) re-issues a token without re-authenticating, as
long as the prior session is still valid. Internally this is a fresh
`create_token` against the existing session record; expired sessions are
rejected.

**Validation.** Every protected request goes through
`AuthMiddleware.dispatch()` (`auth_middleware.py:160+`), which:

1. Skips `AUTH_PUBLIC_PATHS` (`auth_middleware.py:67-89`) - `/health`,
   `/.well-known/...`, the login flow itself.
2. Decodes the bearer token via `JWTManager.verify_token()`
   (`jwt_tokens.py:78-93`) which returns `None` on bad signature or
   expiry.
3. Resolves the user (operator) or identity (agent), populates
   `request.state.user` / `request.state.identity`, and enforces
   `task_ids` scoping for `/tasks/{id}/{complete,fail,progress,cancel,
   block,steal}` paths (`auth_middleware.py:55`).
4. Returns `JSONResponse(401)` on any verification failure.

**Revocation.**

- Operators: `POST /auth/logout` calls `AuthService.logout(session_id)`
  (`routes/auth.py:424-435`), which sets `session.revoked = True` in the
  session store. Subsequent requests with the same JWT fail validation.
- Agent identities: `POST /identities/{id}/revoke`
  (`routes/identities.py:91-103`).
- Cluster nodes: `ClusterAuthenticator.revoke_token()` /
  `revoke_node()` (`cluster_auth.py:174-191`).

Operators with the `auth:manage` permission can also force-logout other
users via `DELETE /auth/users/{id}` (`routes/auth.py:499-520`).

## RBAC

Three built-in roles in strict privilege order
(`core/security/auth.py:81-87`):

- **admin** - full access, including `auth:manage`, `config:write`,
  `admin:manage` (which gates shutdown/broadcast/drain), `agents:kill`.
- **operator** - task and agent management, no config or user changes.
  Can write tasks, kill agents, manage cluster nodes, post to bulletin.
- **viewer** - read-only access to tasks, agents, status, costs,
  bulletin.

Per-role permission table
(`core/security/auth.py:90-139`):

| Permission         | admin | operator | viewer |
| ------------------ | :---: | :------: | :----: |
| `tasks:read`       |  yes  |   yes    |  yes   |
| `tasks:write`      |  yes  |   yes    |  no    |
| `tasks:delete`     |  yes  |   no     |  no    |
| `agents:read`      |  yes  |   yes    |  yes   |
| `agents:write`     |  yes  |   yes    |  no    |
| `agents:kill`      |  yes  |   yes    |  no    |
| `cluster:read`     |  yes  |   yes    |  yes   |
| `cluster:write`    |  yes  |   no     |  no    |
| `config:read`      |  yes  |   no     |  no    |
| `config:write`     |  yes  |   no     |  no    |
| `auth:manage`      |  yes  |   no     |  no    |
| `webhooks:manage`  |  yes  |   no     |  no    |
| `costs:read`       |  yes  |   yes    |  yes   |
| `bulletin:read`    |  yes  |   yes    |  yes   |
| `bulletin:write`   |  yes  |   yes    |  no    |
| `admin:manage`     |  yes  |   no     |  no    |

`admin:manage` is the kill-switch: shutdown, broadcast, drain, and the
config writer all require it. Only ADMIN holds it by design
(`core/security/auth.py:109-113`).

RBAC is enforced at the route level by `RBACEnforcer`
(`core/security/rbac.py:118-...`), which maps URL prefixes + HTTP
methods to required permissions. Default rules
(`core/security/rbac.py:79-115`) cover `/auth/users`, `/config`,
`/webhooks`, `/cluster`, `/agents`, `/tasks`, `/bulletin`, `/costs`,
`/status`, `/health`. Order matters - first match wins - and additional
rules can be passed in via `RBACEnforcer(extra_rules=...)`.

To add a custom rule, append a `RoutePermission(path_prefix, method,
permission)` (`rbac.py:64-76`) to the enforcer's extra rules at server
startup. Mention the new permission in `_ROLE_PERMISSIONS` if existing
roles should hold it; otherwise it is denied by default.

### Policy engine

For decisions that go beyond simple route-level RBAC - for example "ask
human before letting an agent edit `migrations/`", or "deny secret-file
edits regardless of role" - Bernstein has a layered policy engine
(`core/security/policy_engine.py`). It evaluates `PermissionDecision`
records in this precedence order (`policy_engine.py:29-36`):

1. **DENY** - mandatory block, bypass-immune.
2. **IMMUNE** - safety-critical paths (e.g. `.git`, key files), bypass-immune.
3. **SAFETY** - secret detection, bypass-immune.
4. **ASK** - requires human approval (surfaces in `/approvals/queue`).
5. **ALLOW** - permitted to proceed.

YAML rules live under `policy:` in `bernstein.yaml` and are loaded by
`PolicyEngine`; optional Rego rules can be merged in via the OPA
integration in `policy_engine.py`. The engine is also where command
allowlists (`command_allowlist.py`), DLP scanning (`dlp_scanner.py`,
`dlp_scanner_v2.py`), and PII output gates (`pii_output_gate.py`) plug
in.

### Multi-tenant isolation

Source: `core/security/tenant_isolation.py`,
`core/security/tenanting.py`. Bernstein supports tenant-scoped data
paths (`tenant_isolation.py:1-5`) where every tenant gets its own
`.sdd/{tenant_id}/{backlog,metrics,runtime/wal,audit}` subtree
(`tenant_isolation.py:44-60`). All task queries, WAL writes, and
audit-log writes are filtered by tenant ID, and tenant resolution
happens at the API edge via `request_tenant_id()` /
`resolve_tenant_scope()` (`core/tenanting.py`).

When auth is configured, tenant scoping is automatic from JWT claims;
unauthenticated dev mode falls back to `DEFAULT_TENANT_ID`. Operators
audit cross-tenant leakage with `tenant_isolation_verify.py` and rate-
limit per-tenant via `tenant_rate_limiter.py`.

## Identities API

Agent identities are how Bernstein implements zero-trust spawning. Every
agent gets its own JWT with explicit `task_ids` and `permissions`; the
auth middleware refuses to let an agent mutate a task it wasn't issued
for. The identities surface lives at `core/routes/identities.py`.

| Endpoint                                      | Purpose                                                                                            | Code                                  |
| --------------------------------------------- | -------------------------------------------------------------------------------------------------- | ------------------------------------- |
| `GET /identities`                             | List identities. Filters: `status`, `role`. Returns `id`, `role`, `session_id`, `status`, `permissions`, `created_at`, `parent_identity_id`. | `routes/identities.py:35-64`          |
| `GET /identities/{id}`                        | Full identity record (credential hash redacted before serialisation).                              | `routes/identities.py:72-83`          |
| `POST /identities/{id}/revoke`                | Revoke an agent identity. Body `{reason: "..."}`. Future requests with the identity's JWT fail.    | `routes/identities.py:91-103`         |
| `GET /identities/{id}/audit`                  | Per-identity audit trail. Returns the identity's events from the audit store. `?limit=100` default. | `routes/identities.py:111-122`        |

Backing store: `core/agent_identity.py` (`AgentIdentityStore`) under
`.sdd/auth/`. The store is created lazily on first request
(`routes/identities.py:17-27`). Credentials are stored hashed; the API
strips them before responses (`:82`).

## Install fingerprint (v1.0)

A separate identity surface, off by default, lives at
`core/identity/install_rev.py`. It produces an 80-bit base32 token
(16 chars) per install via HMAC-SHA256 over `operator_seed ||
install_nonce || version_major`. The token is emitted in three slots:
a `# bernstein-rev:` comment in YAML configs, a top-level `_rev` field
in trace JSONL, and a `<!-- bernstein-rev: -->` footer in role-prompt
markdown. The slots are independent so a typical copy-paste round
preserves at least one of them.

The seed is operator-controlled and never ships to end users; the
nonce is a random 80-bit value persisted at `~/.bernstein/install_nonce`.
Without the seed, an end-user install cannot mint tokens that match
the operator's verifier. There is no telemetry - bernstein never
opens a network connection to phone home install state.

Kill switch: `BERNSTEIN_DISABLE_IDENTITY=1` short-circuits every
emit site and returns the fixed sentinel `0000000000000000`.

## Audit log

The audit log is **append-only, daily-rotated, and HMAC-chained**
(`core/security/audit.py:1-15`). Every event embeds an HMAC computed
over the previous event's HMAC and the current event's payload, forming a
hash chain that breaks if any record is rewritten or deleted.

**Storage.** One JSONL file per UTC day in `.sdd/audit/YYYY-MM-DD.jsonl`.
Default retention: 90 days (`DEFAULT_RETENTION_DAYS`, `audit.py:40`). Retention
is configured programmatically by passing `RetentionPolicy(retention_days=N,
archive_subdir="archive")` to `AuditLog.archive(...)`; there is no environment
variable or config-file key for it. Files older than the retention window are
gzip-compressed into `.sdd/audit/archive/YYYY-MM-DD.jsonl.gz` by
`AuditLog.archive`. Archived segments remain first-class chain links:
`AuditLog.verify` (and `bernstein audit verify` / `verify-hmac`) replay the
archived `.gz` segments in date order before the live files, so the chain
verifies end to end across the archive boundary:

```shell
# Verify the full HMAC chain, including archived segments.
bernstein audit verify-hmac
```

Do **not** hand-prune or rename files under `archive/`: removing a segment
breaks the chain linkage, and a deleted or byte-edited segment is reported as
a verification failure naming that segment.

**Key handling.** The HMAC key lives **outside** the audit directory so an
attacker with write access to the JSONL files cannot also read or rotate
the signing key (`audit.py:6-14`). Default location:
`$XDG_STATE_HOME/bernstein/audit.key`, falling back to
`~/.local/state/bernstein/audit.key`. Override with the
`BERNSTEIN_AUDIT_KEY_PATH` environment variable (`audit.py:43`). The key
file is **required** to be mode `0600`; group- or world-readable keys
fail at load time (`audit.py:71-86`).

**Integrity verification.** On orchestrator startup,
`audit_integrity.py:DEFAULT_VERIFY_COUNT=100` events are walked and their
HMAC chain re-checked (`core/security/audit_integrity.py:1-30`). Failures
produce structured warnings that can be alerted on. To force a full check
across all entries, call the helpers in `audit_integrity.py` directly.

**Querying and export.** `GET /audit` (`core/routes/audit_log.py:92-...`)
supports filtering by `event_type`, ISO timestamp range (`from`, `to`),
full-text search, and pagination (`page`, `page_size` up to 200). The
filter logic is `audit_log.py:40-74`.

For SOC 2 evidence collection, pair `GET /audit` with `GET
/identities/{id}/audit` for per-identity views, and verify the HMAC chain
out-of-band before exporting. See [Audit and SOC 2 evidence](
../security/AUDIT.md) for the compliance narrative.

## Drain and export

These are operator-side primitives intended for graceful shutdown,
incident response, and compliance evidence export.

**Drain** (`core/routes/drain.py`): freeze new task claiming so existing
agents finish without picking up new work. Three endpoints:

| Endpoint              | Effect                                                                                  |
| --------------------- | --------------------------------------------------------------------------------------- |
| `POST /drain`         | Sets `app.state.draining = True`. Response includes `active_agents` (claimed tasks).    |
| `POST /drain/cancel`  | Resets `draining = False`.                                                              |
| `GET /drain`          | Returns current draining flag and active-agent count.                                   |

The orchestrator's task-claim path checks `app.state.draining` and
refuses to assign new work while it is set. Combine with
`/cluster/nodes/{id}/drain` for a multi-node graceful shutdown - see
[Cluster mode](cluster-mode.md).

**Export** (`core/routes/export.py`):

| Endpoint                       | Purpose                                                                            |
| ------------------------------ | ---------------------------------------------------------------------------------- |
| `GET /export/tasks?format=csv` | All tasks as CSV or JSON (default). Fields: `id`, `title`, `description`, `role`, `priority`, `status`, `assigned_agent`, `created_at`, `completed_at` (`export.py:23-33`). |
| `GET /export/agents?format=csv`| Agent snapshot from `.sdd/runtime/agents.json`. Fields: `id`, `role`, `status`, `task_id`, `started_at` (`export.py:35-41`). |

Both endpoints stream as `Content-Disposition: attachment` so they're
safe to bookmark from a browser.

## SBOM generation

`core/routes/sbom.py` exposes on-demand Software Bill of Materials
generation for supply-chain compliance.

- `POST /sbom/generate` - produce a CycloneDX or SPDX JSON SBOM from
  installed packages, optionally run vulnerability scanning via
  `osv-scanner` or `grype`, and gate the response on critical findings
  (`sbom.py:122-214`). Body fields: `sbom_format`, `source`, `run_scan`,
  `block_on_critical`. Response 422 when `block_on_critical=true` and
  any CRITICAL vulnerability is present.
- `GET /sbom/artifacts` - list previously generated SBOM JSON artifacts
  from `.sdd/artifacts/sbom/` (`sbom.py:217-247`).

Generator implementation: `core/security/sbom.py` (`SBOMGenerator`,
`SBOMVulnerabilityGate`). For scheduled SBOM emission and CI integration,
the same primitives are exposed as a `bernstein audit` subcommand and as
gates inside the [Quality pipeline](../architecture/quality-pipeline.md).

## Compliance

Bernstein's compliance posture is a composition of the above primitives
plus configurable policy. Rather than duplicate it here, see:

- [Model policy](MODEL_POLICY.md) - model allowlist/denylist, residency,
  cost ceilings, and the cascade-router escalation rules that interact
  with regulated workloads.
- [Audit and SOC 2 evidence](../security/AUDIT.md) - the canonical
  walkthrough of the audit log, integrity proofs, and SOC 2 control
  mapping.
- [Security hardening](../security/security-hardening.md) - sandbox
  hardening, allow-listed commands, and DLP scanning.

Compliance modules in code (`core/security/`):

- `eu_ai_act.py` - EU AI Act risk assessment helpers.
- `hipaa.py` - HIPAA PHI gates.
- `soc2_report.py` - SOC 2 evidence packaging.
- `compliance.py`, `compliance_policies.py`, `compliance_report.py` -
  shared policy engine surfaced by the `bernstein compliance` CLI group
  (`cli/commands/compliance_cmd.py`).

## Code pointers

| Concern                            | File                                                                  |
| ---------------------------------- | --------------------------------------------------------------------- |
| Auth middleware (every request)    | `src/bernstein/core/security/auth_middleware.py`                      |
| AuthService, RBAC, role table      | `src/bernstein/core/security/auth.py`                                 |
| RBAC route enforcement             | `src/bernstein/core/security/rbac.py`                                 |
| JWT manager                        | `src/bernstein/core/security/jwt_tokens.py`                           |
| OIDC / SAML / device flow routes   | `src/bernstein/core/routes/auth.py`                                   |
| Agent identities API               | `src/bernstein/core/routes/identities.py`                             |
| Agent identity store               | `src/bernstein/core/agent_identity.py`                                |
| Audit log (HMAC chain)             | `src/bernstein/core/security/audit.py`                                |
| Audit integrity verifier           | `src/bernstein/core/security/audit_integrity.py`                      |
| Audit query / search routes        | `src/bernstein/core/routes/audit_log.py`                              |
| Drain endpoints                    | `src/bernstein/core/routes/drain.py`                                  |
| Export endpoints                   | `src/bernstein/core/routes/export.py`                                 |
| SBOM endpoints                     | `src/bernstein/core/routes/sbom.py`                                   |
| SBOM generator                     | `src/bernstein/core/security/sbom.py`                                 |
| Cluster JWT auth                   | `src/bernstein/core/protocols/cluster/cluster_auth.py`                |
| Compliance frameworks              | `src/bernstein/core/security/{eu_ai_act,hipaa,soc2_report}.py`        |
| OAuth / SSO config                 | `src/bernstein/core/security/sso_oidc.py`, `oauth_pkce.py`            |
| Vault / secrets                    | `src/bernstein/core/security/vault/`, `vault_injector.py`             |
| Tenant isolation                   | `src/bernstein/core/security/tenant_isolation.py`, `tenanting.py`     |
| Permission modes (Claude profiles) | `src/bernstein/core/security/{permission_mode,permission_matrix,permission_rules}.py` |
