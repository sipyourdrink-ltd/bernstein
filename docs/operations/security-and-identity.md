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
- **Agent identity** - `core/identity/agent_jwt.py` issues task-scoped JWTs
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
   `request.state.user` / `request.state.identity`, and checks the
   permission the requested route declares (see *Route permissions* below).
4. Enforces `task_ids` scoping on every mutating request carrying an agent
   identity. The rule is applied to the task the request acts on, not to a
   list of blessed URLs, so it holds wherever that id comes from (see the
   table below).
5. Returns `JSONResponse(401)` on any verification failure, `403` when the
   credential authenticates but does not hold the route's permission.

**Route permissions.** Every credential that authenticates is gated on the
permission `_get_required_permission(path, method)` resolves for the route,
before the request reaches a handler. Each kind is checked against the
authority it actually carries, and gets `403` when the route names one it
does not hold:

| Credential | Checked against | Reaches |
| --- | --- | --- |
| SSO user JWT | the RBAC role's permissions | whatever the role grants |
| Agent identity token | the signed permission set the token pins, plus `_AGENT_PERMISSION_EQUIVALENTS` | its own task work; not `/agents/*`, `/cluster/*` or `/bulletin` unless the grant says so |
| Cluster worker secret | `_CLUSTER_SECRET_PERMISSIONS`, a fixed set | `cluster:write` / `cluster:read`, `tasks:write` / `tasks:read`, `status:read` — and nothing else |
| Legacy static bearer | nothing — it is the operator credential | everything |

Reads are gated as well as writes. A read route's declared permission is
what keeps one agent's log and stream output out of another agent's reach,
and it is the only thing that bounds a read at all: the separate
operator-only (`admin:manage`) refusal that agent tokens and the cluster
secret have always carried runs on non-read methods only.

*Agent identities.* Agent grants use a narrower vocabulary than the route
map, and spell the per-task write authority `tasks:claim` where the route
map says `tasks:write`. The two names denote the same authority and are
resolved through `_AGENT_PERMISSION_EQUIVALENTS` in `auth_middleware.py`, so
a worker still claims, progresses, completes and decomposes its own tasks —
bounded by `task_ids` as before. Nothing else in the two vocabularies is
treated as equivalent: a new agent-reachable surface needs its permission
granted outright in `AGENT_ROLE_PERMISSIONS`.

*Cluster worker secret.* This credential has no record of its own to hang a
grant on — it is one string handed to every node in the fleet, so it cannot
be revoked per worker or narrowed per task. Its authority is therefore fixed
in the middleware at what joining and working a cluster needs: the
`/cluster` surface, the task pull-and-report cycle, and the `status:read`
floor. It reaches neither the agent log, stream and session-kill routes nor
the bulletin, `/auth` or `/webhooks`, because a worker drives its own agents
through the local spawner and never touches those over HTTP. Widening this
set widens it for the whole fleet at once; a deployment that needs one
credential to do more should use an SSO user with the role that grants it.

**Two layers on the cluster routes.** The middleware is the only gate on
most paths. `_verify_cluster_auth` in `routes/task_cluster.py` adds a second,
scope-checked layer, and it covers the mutating `/cluster/*` routes only —
registration and heartbeat (`node:register` / `node:heartbeat`), claim
gossip (`node:heartbeat`), and the node-registry mutations cordon, uncordon,
drain, unregister and steal (`node:admin`). `POST /cluster/steal` reassigns
other nodes' claimed work from caller-reported queue depths, so it is scoped
with the other node-registry mutations rather than with gossip, whose bearer
scope only has to establish fleet membership because each receipt carries
its own Ed25519 signature. For `/tasks/*`, `/agents/*` and `/bulletin` there
is no inner layer at all.

**Where the task id comes from.** A rule enforced on only some of these is
enforced on an arbitrary subset, so all of them are covered:

| How the request names a task | Enforced by |
| --- | --- |
| `/tasks/{id}` and anything below it, plus the `/api/v<n>` mirror | deny-by-default path gate; a task route added later is covered without editing the middleware |
| `{task_id}` in the path under any other prefix (`/approvals/{task_id}/approve`, the review board's per-task decision route) | matchers compiled from the app's own route table (`task_id_route_patterns`), so they cannot drift from the routes it registers |
| ids in a request body on a `/tasks/` collection route (`batch-ops`, `claim-batch`, `self-create`) | `enforce_agent_task_scope_for_ids` in the handler |
| a body-carried id outside `/tasks/` (`POST /a2a/message`) | `enforce_agent_task_scope_for_ids` in the handler |
| an id the handler resolves from another key (the task behind an ACP run, the tasks a plan decision transitions, the tasks a cluster steal reassigns) | `enforce_agent_task_scope_for_ids` on the resolved ids, before the mutation |

The only exemptions are the collection routes under `/tasks/`
(`TASK_COLLECTION_SEGMENTS` in `auth_middleware.py`), which address the
collection rather than one task, and the claim-next routes
(`GET /tasks/next/{role}`, `POST /tasks/claim-receipt`), where the server
picks the row and the caller cannot name a task. A token with an empty
`task_ids` claim is an unrestricted manager token, and non-agent
credentials never reach the check at all.

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
| `scim:read`        |  yes  |   no     |  no    |
| `scim:write`       |  no   |   no     |  no    |

`admin:manage` is the kill-switch: shutdown, broadcast, drain, and the
config writer all require it. Only ADMIN holds it by design
(`core/security/auth.py:109-113`).

`scim:write` is held by no role: the SCIM surface serves reads only, so
nothing may hold the authority to reach a write route that does not exist.
A write slice adds the routes and the grant together.

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

When auth is configured, tenant scoping is automatic from the credential:
`SSOAuthMiddleware` binds the tenant a validated credential was issued for
onto the request, and `request_tenant_id()` reports that binding.

| Credential | Bound tenant | May select another tenant |
|---|---|---|
| SSO user JWT | `tenant_id` claim, else `default` | only with `admin:manage` |
| Agent identity JWT | the credential's own `tenant_id` | no |
| Legacy static bearer | `default` | no |
| Cluster worker secret | `default` | no |
| Dashboard session / scoped token | `default` | no |
| HMAC webhook secret | `default` | no |

`X-Tenant-Id` is a *requested* scope, not an identity: `resolve_tenant_scope()`
authorizes it against the bound scope, so naming your own tenant is granted and
naming a different one needs the operator scope (`admin:manage`). Credentials
that are a single process-wide string carry no tenant of their own, so they bind
to `default` and stay there — administering several tenants from one credential
is the SSO `admin` user's job, where the grant is per-user and revocable.
Note that `default` is a tenant like any other, not a wildcard.

Unauthenticated dev mode (`BERNSTEIN_AUTH_DISABLED`) is the one mode where
`X-Tenant-Id` is itself the bound scope, falling back to `DEFAULT_TENANT_ID`
when absent — with auth off there is no credential to derive a scope from.

Scope: the binding above establishes *which* tenant a request is authorized for.
Applying it is per-route — the task CRUD, `/costs`, `/costs/live`, the task export
and GraphQL front doors, and the dashboard and observability readers (`/status`,
`/status/duration-predictions`, `/dashboard/data`, `/dashboard/team`, `/badge.json`,
`/recap`, `/observability/deps`, `/observability/agents`,
`/observability/token-breakdown`, `/agents`, `/agents/comparison`, `/export/agents`)
resolve the scope through `resolve_tenant_scope()`, and the store filters by the
tenant it is given. Routes that aggregate process-global data or look rows up by ID
without passing a tenant are not scoped by this binding; treat them as operator
surfaces until they are converted.

Runtime records carry no tenant of their own. The agent snapshot
(`.sdd/runtime/agents.json`) and the token sidecars beside it are written by one
orchestrator process serving every tenant it is configured for, so the readers over
them derive the tenant from the task each record names rather than from a field in
the file. A record naming no task, or naming one that no longer resolves, has no
tenant to derive and is left in place.

Aggregate figures follow the rows they are computed from. `TaskStore.status_summary()`
takes an optional `tenant_id`; when one is given, the counts and the cost totals come
from that tenant's rows alone, and the untenanted per-role metrics file is not folded
in — attributing an unattributable total to whichever tenant asked for it would report
somebody else's spend as theirs. Called without a tenant it keeps the whole-store
roll-up, which is what the CLI, the TUI and the supervisor read.

Budget figures follow the same scope. A run's cost file records the spend of
every tenant that spent against it, and the caps stored beside it bound the run
as a whole — so a tenant-scoped read reports the tenant's spend against the
tenant's configured `budget_usd`, not the run's. Where no tenants are
configured the run and the scope are the same thing and the run's caps stand.
The retained-usage limit applies as before: a cost file holds the usage buffer
(`BERNSTEIN_COST_USAGE_BUFFER`, 10 000 rows by default), so on a long run these
figures cover the retained window, and full history lives in the rotation files
when `rotation_dir` is configured.

Operators audit tenant leakage with `tenant_isolation_verify.py` and rate-limit
per-tenant via `tenant_rate_limiter.py`.

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

Backing store: `core/identity/agent_jwt.py` (`AgentIdentityStore`) under
`.sdd/auth/`. The store is created lazily on first request
(`routes/identities.py:17-27`). Credentials are stored hashed; the API
strips them before responses (`:82`).

### Unreadable identity records

A credential's persisted `tenant_id` is read back as the scope every request
that credential authenticates is served under, so the store requires it to be
a real tenant id rather than coercing whatever is on disk into one.

The distinction is key presence, not emptiness:

| Stored | Read as | Why |
|---|---|---|
| key absent | `default` | written before the field existed — the upgrade path |
| `"acme"` / `"  acme  "` | `acme` | normalized |
| `null` | corrupt | something wrote a tenant and wrote a non-tenant |
| `""` / `"   "` | corrupt | a blank is not a tenant |
| `42` / `true` / `[…]` / `{…}` | corrupt | coercing it would invent a scope |

An explicit `null` is deliberately *not* treated as an omitted key. Only the
absent key is the legacy case; a key that is present carries an assertion
about scope, and a null assertion is refused rather than authenticated under
`default`.

The same requirement applies to the three collection fields that carry what an
agent may do — `permissions`, `task_ids` and `allowed_files`, on the identity
and on `credential` alike:

| Stored | Read as | Why |
|---|---|---|
| key absent | `[]` | the field defaults to empty |
| `["a", "b"]` | `["a", "b"]` | the canonical form `create_identity` writes |
| `null` | corrupt | a null is not a list |
| `"admin:manage"` | corrupt | a string would otherwise yield its characters |
| `{"admin:manage": 1}` | corrupt | a mapping would otherwise yield its keys, granting them |
| `{}` | corrupt | an empty mapping would otherwise read as "no restriction" |
| `[1]` / `[null]` | corrupt | coercing an entry would invent a task id or a permission |

An empty *list* is not corrupt, and it is not "no data": for `task_ids` and
`allowed_files` it means **no restriction**. That is exactly why the shapes
above are refused rather than coerced — each of them collapses to an empty
list, which widens a scoped credential into an unscoped one.

`task_ids` and `allowed_files` are stored twice, once on the identity and once
on its credential, and the two copies must agree. Different consumers read
different copies — the request middleware reads the identity's, the JWT claim
check reads the credential's — so a record carrying two answers is refused
rather than authenticated under whichever is read first. `create_identity`
writes the same list to both, so a mismatch means the record was hand-edited or
written by something else.

A corrupt record is skipped, never fatal: `GET /identities` leaves it out, the
startup token-index scan skips it instead of failing to boot, and a request
presenting its token is answered `401` like any other unrecognised token —
not `500`. The same applies to a file that is not valid JSON, is not a JSON
object at all, or cannot be read. Each skip logs
`Skipping corrupt identity file: <path>`.

Calling `AgentIdentity.from_dict()` / `AgentCredential.from_dict()` directly
raises `ValueError` on the same records; the store is what turns that into a
skip. `create_identity()` applies the same check to its `task_ids` and
`allowed_files` arguments before the token is signed, so a bad scope is refused
at the call rather than becoming a credential nobody can load. It cannot repair
records already on disk — for those, use the repair below.

Operator repair, for a record hand-edited or written by an external tool:

1. Find the path in the warning, under `.sdd/auth/agent_identities/`.
2. Set `credential.tenant_id` to the tenant the agent belongs to, or delete the
   key to place it in `default`.
3. Make `permissions`, `task_ids` and `allowed_files` JSON arrays of strings, or
   delete the keys to read as empty. Where `task_ids` and `allowed_files` appear
   both on the identity and on `credential`, make the two copies match; take the
   credential's copy as authoritative, since that is the one the issued token
   was signed with.
4. No restart is needed — the store reads each record on demand — but a running
   server keeps a token index built at startup, so restart it if the repaired
   identity uses an opaque token.

Revoking and re-spawning the agent is always a valid alternative: identities are
per-session and cheap to reissue.

### `allowed_files` contains, it does not prevent

`allowed_files` is recorded on the credential, signed into the token, and
checked for agreement between the two on every JWT authentication — so it is
part of what makes a token that token. It is **not** consulted when an agent
writes a file. No write, staging, or completion path reads it; individual
writes are bounded by the sandbox and by the worktree the agent is confined
to, and task authority is bounded by `task_ids`.

It **is** consulted where the agent's work is accepted into the repository.
The merge acceptance gate compares the file list the merge would bring in
against the signed scope, and refuses the merge when any path falls outside
it (`core/agents/spawner_merge.py`). State the difference plainly, because it
matters when reasoning about a compromised agent:

- The out-of-scope write **still happens on disk**, inside the agent's own
  worktree.
- It **does not reach the repository**. The merge is refused, the refusal is
  recorded to `.sdd/runtime/refused_merges.jsonl`, and the branch is left
  intact so an operator can inspect what was refused.

That is a containment boundary, not a prevention boundary. Where you need a
write to be impossible rather than unmergeable, bound it with the sandbox.

**The patterns.** Repository-relative globs, in the same namespace
`git diff --name-only` prints. `**` crosses directories and a single `*` does
not, so `src/*` admits the files directly under `src/` and `src/**` admits the
tree. A pattern is not a prefix: `src` admits the path `src` and nothing
beneath it.

**The empty list still means no restriction.** Every credential minted before
this gate existed carries `[]`, so the gate stays inert until an operator sets
a scope — that is also the migration. A session with no identity record at all
is likewise unrestricted: there is no signed scope, so there is nothing to
enforce.

Absolute paths, drive-qualified paths, home-directory references and patterns
that walk out of the root are refused when the identity is created, so they
never become a signed scope. A pattern already on disk that cannot be
interpreted matches nothing, and the refusal names it — an unreadable scope
must not widen into "no scope".

**A damaged record refuses; a missing one does not.** The two are not the same
evidence. No record means no scope was ever declared for that session. A record
that is on disk and does not load — truncated by an interrupted write, hand-
edited so its two copies of the scope disagree, or sitting in a directory the
orchestrator cannot list — is a scope someone declared that the gate cannot
read, and it refuses rather than merging unbounded. The refusal is journalled
under `allowed-files-unreadable` instead of `allowed-files-scope`, so
`.sdd/runtime/refused_merges.jsonl` distinguishes "the agent went outside its
scope" from "the scope itself needs repair". Repair it with the identity
commands above, or revoke and re-spawn the agent.

**An unreadable change refuses too.** The gate reads the file list a merge
would bring in, and an empty list means the merge touches nothing — the same
answer a `git diff` that failed would otherwise give. So a read that does not
complete refuses instead of returning nothing to judge, journalled under
`allowed-files-diff-unreadable`. The blast-radius ceiling refuses the same
change under `blast-radius-unreadable`, for the same reason: an empty change
scores as the safest possible one.

The case worth knowing about is not the missing branch, where the merge would
fail anyway. It is the timeout: the file list is read with a 30-second budget
the merge itself does not share, so a large enough diff can time out here
while `git merge` still succeeds. Both gates stay inert when nothing was
asked of them — no ceiling set, no scope declared — so only a failed read
inside a gate that is actually on can turn into a refusal.

## SCIM 2.0 provisioning surface (read-only)

An identity team's directory already speaks SCIM 2.0, so the orchestrator
serves it rather than asking anyone to write an adapter. The surface is JSON
over HTTP (RFC 7643 schema, RFC 7644 protocol) - no client library is
involved. It lives at `core/routes/scim.py` and mounts under `/scim/v2`.

Today it serves reads only. Every principal it returns is the same agent
identity the Identities API serves; there is one store, read two ways.

| Endpoint | Purpose |
| --- | --- |
| `GET /scim/v2/ServiceProviderConfig` | RFC 7643 §5 discovery. Reports only what is mounted. |
| `GET /scim/v2/Schemas`, `GET /scim/v2/Schemas/{urn}` | RFC 7643 §7 schema resources for the attributes actually projected. |
| `GET /scim/v2/ResourceTypes`, `GET /scim/v2/ResourceTypes/{id}` | RFC 7643 §6. Only resources that have an endpoint. |
| `GET /scim/v2/Users`, `GET /scim/v2/Users/{id}` | Agent principals as SCIM `User` resources, in the RFC 7644 §3.4.2 `ListResponse` envelope. |

`GET /scim/v2/Users` accepts `startIndex` and `count` (page size capped at
200). `filter` is answered with `501` and `scimType: invalidFilter` because
`ServiceProviderConfig` reports `filter.supported = false`; returning the
unfiltered list would look like a successful narrow query.

**Deletion semantics.** A SCIM client expects `DELETE` to remove a resource.
The record kept here is append-only, so a principal removed upstream becomes
inactive while the record of its existence and of its removal stays. That is
declared now, under
`urn:ietf:params:scim:schemas:extension:bernstein:2.0:ServiceProviderConfig`,
rather than met as a surprise once the write surface exists:

```json
"delete": { "supported": false, "semantics": "soft", "retainsHistory": true }
```

**Access.** Reads need `scim:read`, writes `scim:write`. The requirement is
declared in the same two places every other route uses - the rule table in
`core/security/rbac.py` and the prefix map the middleware consults in
`core/security/auth_middleware.py` - so a credential issued to a directory for
provisioning satisfies no other route's requirement, and a plain `status:read`
viewer cannot list principals.

## Delegation capability tokens

When a run fans out, authority fans out with it. A **capability token** makes
each delegation hop a signed, scope-attenuating grant, so the
`principal -> orchestrator -> sub-agent` authority chain becomes a single
offline-verifiable structure. Backing module: `core/security/capability_tokens.py`.

Each token is an Ed25519 **detached JWS** (RFC 7515) over the JCS-canonical
(RFC 8785) token body, with a token-specific `typ` (`delegation-capability+jws`)
so a signature minted for an agent card cannot be replayed as a token. The token
binds both its own `issuer_pubkey` and its delegatee's `subject_pubkey` captured
at mint time, so **key rotation never invalidates historical tokens**. Signing
and key resolution reuse `agent_card_signer.py` and `agent_card_keystore.py`.

**Tokens narrow, never grant.** `attenuate(parent, ...)` enforces that a child's
caveats are a subset of its parent's over every axis:

| Caveat            | Subset rule                                                            |
| ----------------- | --------------------------------------------------------------------- |
| `permissions`     | set-subset over the `PERM_*` vocabulary                               |
| `task_ids`        | allowlist subset (`None` = unconstrained/widest)                     |
| `path_prefixes`   | POSIX ancestor-or-equal coverage (`/a/b` covers `/a/b/c`, not `/a/bc`) |
| `not_after`       | expiry no later than the parent                                       |
| `max_uses`        | no greater than the parent (`None` = unlimited/widest)               |
| `remaining_depth` | **strictly less** than the parent (the `max_depth` caveat)            |

Widening at any hop is rejected at mint time *and* independently at verify time,
so a re-signed, structurally-continuous but widened hop still fails from the
signed bytes alone. Approval-gated actions are deliberately **not** expressible
as caveats: a token answers "was this sub-agent granted more than its parent
held?", never "may it perform an action requiring fresh approval?" - that
escalation stays on the approval-receipt surface.

Every mint anchors a `delegation_minted` event into the HMAC audit chain
(`audit_chain.py`), whose `token_hash` and embedded `prev_chain_digest`
cross-reference the token's identity and captured `audit_head` - so
`bernstein audit verify` also attests the mint happened at a fixed chain
position. `PermissionDelegator.verify_capability` verifies the offline chain
first and consults the in-process registry only for liveness (expiry) and
revocation; existing enum-scope callers keep working via `enum_to_caveats`.

`verify_chain` walks the chain root -> leaf with **no network and no registry**:
per-hop signature, structural `parent_token_hash` linkage, identity and pubkey
continuity (the issuer of hop N is the subject of hop N-1), monotonic
attenuation, and root trust-anchor membership. `to_actor_claims` projects a
*verified* chain as nested RFC 8693 `act` claims for external IdP tooling and
refuses an unverified chain.

Verify a chain offline from the CLI:

```
bernstein delegation verify-token chain.json --trust-anchor principal.pem
```

It prints per-hop PASS/FAIL plus the resolved authority path and exits non-zero
on any failing hop. (This is distinct from `bernstein delegation verify <run>`,
which reconstructs the per-hop HMAC *receipt* chain - the ACT log - in
`core/identity/delegation.py`.)

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
| SCIM 2.0 provisioning surface      | `src/bernstein/core/routes/scim.py`                                   |
| Agent identity store               | `src/bernstein/core/identity/agent_jwt.py`                            |
| Delegation capability tokens       | `src/bernstein/core/security/capability_tokens.py`, `permission_delegation.py` |
| Delegation verify CLI              | `src/bernstein/cli/commands/delegation_cmd.py`                        |
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
