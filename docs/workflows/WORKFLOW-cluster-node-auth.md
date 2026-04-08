# WORKFLOW: Cluster Node Registration Auth Hardening (ENT-002)
**Version**: 1.1
**Date**: 2026-04-08
**Author**: Workflow Architect
**Status**: Review
**Implements**: ENT-002 (plans/strategic-300.yaml)

---

## Overview

Cluster node registration hardening adds JWT-based authentication to the node registration and heartbeat endpoints. Without this, a malicious actor could register a fake node and steal tasks from the cluster. The workflow covers token issuance, scope-based authorization, token verification on every cluster operation, revocation, and the heartbeat client's auth integration.

---

## Actors

| Actor | Role in this workflow |
|---|---|
| Cluster Admin | Configures the shared secret, enrolls initial bootstrap tokens |
| Worker Node (NodeHeartbeatClient) | Registers with the central server, sends periodic heartbeats |
| Central Server (FastAPI routes) | Validates JWT tokens on every cluster endpoint |
| ClusterAuthenticator | Issues, verifies, and revokes JWT tokens |
| AuthenticatedNodeRegistry | Wrapper that enforces auth before delegating to NodeRegistry |
| NodeRegistry | In-memory registry with optional disk persistence |
| JWTManager | Signs and verifies HMAC-SHA256 JWT tokens |
| TokenRefreshScheduler | Proactively refreshes tokens before expiry |

---

## Prerequisites

- Cluster auth secret configured (either via `ClusterAuthConfig.secret` or environment variable)
- Central server running with `cluster_authenticator` in `app.state`
- For worker nodes: bootstrap token or pre-shared secret available for initial registration

---

## Trigger

- **Node registration**: Worker node starts and calls `POST /cluster/nodes`
- **Node heartbeat**: Worker node daemon thread calls `POST /cluster/nodes/{node_id}/heartbeat` every N seconds
- **Node unregister**: Worker node shuts down and calls `DELETE /cluster/nodes/{node_id}`
- **Admin operations**: Operator calls cordon/uncordon/drain endpoints

---

## Workflow Tree

### WORKFLOW A: Node Registration (authenticated)

#### STEP A1: Bootstrap Token Presentation
**Actor**: Worker node (NodeHeartbeatClient)
**Action**: Node sends `POST /cluster/nodes` with `Authorization: Bearer <bootstrap_token>` header. The bootstrap token must carry `node:register` scope.
**Timeout**: 10s (httpx client timeout, cluster.py:457)
**Input**:
```json
{
  "name": "string",
  "url": "string",
  "capacity": { "max_agents": 6, "available_slots": 6, "active_agents": 0, "gpu_available": false, "supported_models": ["sonnet", "opus", "haiku"] },
  "labels": {},
  "cell_ids": []
}
```
**Output on SUCCESS**: `201 Created` with node info + issued session token -> GO TO STEP A2
**Output on FAILURE**:
  - `FAILURE(missing_auth)`: No Authorization header -> HTTP 401 `"Missing Authorization header"`. No cleanup.
  - `FAILURE(invalid_format)`: Malformed Bearer header -> HTTP 401 `"Invalid Authorization header format"`. No cleanup.
  - `FAILURE(invalid_token)`: Token signature invalid or expired -> HTTP 401 `"Invalid or expired token"`. No cleanup.
  - `FAILURE(wrong_scope)`: Token lacks `node:register` -> HTTP 401 `"Token lacks required scope 'node:register'"`. No cleanup.
  - `FAILURE(revoked)`: Token has been revoked -> HTTP 401 `"Token has been revoked"`. No cleanup.
  - `FAILURE(network)`: Connection refused or timeout -> Retry after interval (cluster.py:510). No cleanup.

**Observable states**:
  - Customer sees: N/A (system-to-system)
  - Operator sees: `"Registered new node {id} ({name}) at {url}"` in server logs
  - Database: Node added to in-memory registry; persisted to disk if `persist_path` configured
  - Logs: `[cluster_auth] Issued cluster token for node {id} with scopes [node:register, node:heartbeat]`

---

#### STEP A2: Token Issuance
**Actor**: ClusterAuthenticator.issue_node_token() (cluster_auth.py:70-93)
**Action**: After successful registration, the server issues a new JWT token for the registered node with `node:register` + `node:heartbeat` scopes. Token is:
  - Signed with HMAC-SHA256 using the cluster shared secret
  - Valid for `token_expiry_hours` (default 24h)
  - Bound to the node's ID via `user_id` claim
**Timeout**: <1ms (in-memory crypto)
**Input**: `{ node_id: str, scopes: ["node:register", "node:heartbeat"] }`
**Output on SUCCESS**: Signed JWT token string -> Returned in registration response
**Output on FAILURE**:
  - `FAILURE(crypto_error)`: HMAC signing failure -> Should not happen with valid secret. If it does, HTTP 500.

**Observable states**:
  - Operator sees: Token session_id `node-{id}` tracked in authenticator._node_tokens
  - Logs: `[cluster_auth] Issued cluster token for node {node_id} with scopes {scopes}`

---

### WORKFLOW B: Authenticated Heartbeat

#### STEP B1: Token Verification
**Actor**: `_verify_cluster_auth()` (routes/tasks.py:116-131)
**Action**: Extract Authorization header, verify JWT signature, check expiry, check revocation, require `node:heartbeat` scope
**Timeout**: <1ms
**Input**: `Authorization: Bearer <token>`, required_scope=`node:heartbeat`
**Output on SUCCESS**: Verified `JWTPayload` -> GO TO STEP B2
**Output on FAILURE**:
  - Same failure modes as STEP A1 (missing, invalid, expired, revoked, wrong scope) -> HTTP 401

---

#### STEP B2: Node Identity Verification
**Actor**: AuthenticatedNodeRegistry.heartbeat() (cluster_auth.py:240-267)
**Action**: Verify that the token's `user_id` matches the `node_id` in the URL path. This prevents a valid node from impersonating another node's heartbeat.
**Timeout**: <1ms
**Input**: `{ node_id: str (from URL), payload.user_id: str (from token) }`
**Output on SUCCESS**: Identity matches -> GO TO STEP B3
**Output on FAILURE**:
  - `FAILURE(identity_mismatch)`: Token's user_id != heartbeat node_id -> `ClusterAuthError("Token node_id mismatch: token for '{X}', heartbeat for '{Y}'")` -> HTTP 401. No cleanup.

**Observable states**:
  - Customer sees: N/A
  - Operator sees: Auth error in logs if mismatch detected (potential impersonation attempt)
  - Logs: `[cluster_auth] ClusterAuthError: Token node_id mismatch`

---

#### STEP B3: Heartbeat Processing
**Actor**: NodeRegistry.heartbeat() (cluster.py:134-146)
**Action**: Update node's `last_heartbeat` timestamp. If node was OFFLINE, transition to ONLINE. Optionally update capacity.
**Timeout**: <1ms
**Input**: `{ node_id: str, capacity: NodeCapacity | None }`
**Output on SUCCESS**: Updated `NodeInfo` -> HTTP 200
**Output on FAILURE**:
  - `FAILURE(node_unknown)`: Node ID not in registry -> HTTP 404 `"Node '{id}' not registered"`. Client re-registers on next cycle (cluster.py:493-497).

**Observable states**:
  - Operator sees: Node status ONLINE, last_heartbeat updated
  - Database: In-memory node state updated; saved to disk if status changed
  - Logs: `[cluster] Node {id} heartbeat received`

---

### WORKFLOW C: Authenticated Admin Operations

#### STEP C1: Admin Scope Verification
**Actor**: `_verify_cluster_auth()` with `SCOPE_NODE_ADMIN`
**Action**: Require `node:admin` scope for destructive operations: unregister, cordon, uncordon, drain
**Timeout**: <1ms
**Input**: Authorization header with `node:admin` scope
**Output on SUCCESS**: -> Proceed to admin operation
**Output on FAILURE**:
  - `FAILURE(wrong_scope)`: Regular node token lacks `node:admin` -> HTTP 401. This is the key protection: nodes cannot unregister/cordon other nodes without admin privileges.

**Operations protected by admin scope**:
  - `DELETE /cluster/nodes/{node_id}` — Unregister (also revokes node's tokens)
  - `POST /cluster/nodes/{node_id}/cordon` — Exclude from scheduling
  - `POST /cluster/nodes/{node_id}/uncordon` — Resume scheduling
  - `POST /cluster/nodes/{node_id}/drain` — Start draining

---

### WORKFLOW D: Token Lifecycle

#### STEP D1: Token Refresh (client-side)
**Actor**: TokenRefreshScheduler (jwt_tokens.py:184-320)
**Action**: Proactively refresh JWT token 5 minutes before expiry. Thread-safe with generation counter to handle concurrent refresh attempts.
**Timeout**: N/A (runs on interval check)
**Input**: Current token, generation counter
**Output on SUCCESS**: New token issued, generation incremented, fail_count reset
**Output on FAILURE**:
  - `FAILURE(refresh_failed)`: Token creation fails -> Increment fail_count. Retry on next check.
  - `FAILURE(fatal)`: 3 consecutive failures -> `TokenRefreshFatalError` raised. Node must re-register.

**Observable states**:
  - Operator sees: `JWT refreshed: session={id} generation={N} expires_at={T}` in logs
  - On fatal: `JWT refresh fatal: {N} consecutive failures for session {id}`

---

#### STEP D2: Token Revocation (server-side)
**Actor**: ClusterAuthenticator.revoke_token() / revoke_node() (cluster_auth.py:174-192)
**Action**: Add token to in-memory revocation set. For `revoke_node()`, remove node's session_id mapping.
**Timeout**: <1ms
**Input**: Token string or node_id
**Output on SUCCESS**: Token added to `_revoked_tokens` set; future verification fails
**Output on FAILURE**: None (in-memory set operation)

**Limitation**: Revocation list is **in-memory only**. Server restart clears the revocation set. Revoked tokens with remaining TTL become valid again after restart.

---

### WORKFLOW E: Auth-Disabled Mode

#### STEP E1: Bypass Authentication
**Actor**: ClusterAuthenticator.verify_request() (cluster_auth.py:113-121)
**Action**: When `require_auth=False`, return a synthetic anonymous payload with all allowed scopes. No token verification performed.
**Timeout**: <1ms
**Output**: Synthetic `JWTPayload(session_id="anonymous", scopes=[all_allowed])`

**Use case**: Development, single-node deployments, backward compatibility with pre-auth clusters.

---

## State Transitions

```
[unregistered] -> (POST /cluster/nodes + valid token) -> [registered/online]
[registered/online] -> (heartbeat received) -> [registered/online] (timestamp updated)
[registered/online] -> (heartbeat timeout exceeded) -> [offline]
[offline] -> (heartbeat received) -> [registered/online]
[registered/online] -> (admin: cordon) -> [cordoned]
[cordoned] -> (admin: uncordon) -> [registered/online]
[registered/online] -> (admin: drain) -> [draining]
[draining] -> (admin: uncordon) -> [registered/online]
[any] -> (admin: unregister) -> [removed] (tokens revoked)
[any] -> (client: stop) -> [unregistered] (best-effort unregister)
```

---

## Handoff Contracts

### Worker Node -> Central Server (Registration)
**Endpoint**: `POST /cluster/nodes`
**Payload**:
```json
{
  "name": "string — human-readable node name",
  "url": "string — node's base URL for task routing",
  "capacity": {
    "max_agents": "int — maximum concurrent agents",
    "available_slots": "int — currently available slots",
    "active_agents": "int — currently active agents",
    "gpu_available": "bool — whether GPU is available",
    "supported_models": "list[str] — model IDs this node supports"
  },
  "labels": "dict[str, str] — key-value labels for affinity scheduling",
  "cell_ids": "list[str] — cell memberships"
}
```
**Headers**: `Authorization: Bearer <bootstrap_token>`
**Success response** (201):
```json
{
  "id": "string — assigned node ID",
  "name": "string",
  "url": "string",
  "status": "online",
  "capacity": { "..." },
  "last_heartbeat": "float — unix timestamp",
  "registered_at": "float — unix timestamp",
  "labels": {},
  "cell_ids": []
}
```
**Failure response** (401):
```json
{
  "detail": "string — auth error message"
}
```
**Timeout**: 10s

### Worker Node -> Central Server (Heartbeat)
**Endpoint**: `POST /cluster/nodes/{node_id}/heartbeat`
**Payload**:
```json
{
  "capacity": {
    "max_agents": "int",
    "available_slots": "int",
    "active_agents": "int",
    "gpu_available": "bool",
    "supported_models": "list[str]"
  }
}
```
**Headers**: `Authorization: Bearer <node_token>`
**Success response** (200): NodeResponse (same as registration)
**Failure response**: 401 (auth failure), 404 (node evicted — triggers re-registration)
**Timeout**: 10s

### Admin -> Central Server (Unregister)
**Endpoint**: `DELETE /cluster/nodes/{node_id}`
**Headers**: `Authorization: Bearer <admin_token>` (requires `node:admin` scope)
**Success response**: 204 No Content
**Failure response**: 401 (auth/scope), 404 (node not found)

---

## Cleanup Inventory

| Resource | Created at step | Destroyed by | Destroy method |
|---|---|---|---|
| In-memory node entry | A1 | Unregister (C1) or server restart | `NodeRegistry.unregister()` |
| Persisted node JSON | A1 (if persist_path) | Unregister (C1) | `NodeRegistry._save()` removes from list |
| Node token session | A2 | `revoke_node()` or server restart | Removed from `_node_tokens` dict |
| Revocation set entries | D2 | Server restart | In-memory set cleared (GAP) |
| Heartbeat daemon thread | Client start | Client stop | `threading.Event.set()` + thread.join() |

---

## Reality Checker Findings

| # | Finding | Severity | Spec section affected | Resolution |
|---|---|---|---|---|
| RC-1 | Token revocation list is in-memory only — server restart clears it, making revoked tokens valid again until TTL expires | High | Workflow D, Step D2 | Needs persistent revocation store (Redis, file-based, or DB). Current mitigation: short TTL (24h default). |
| RC-2 | `AuthenticatedNodeRegistry` is defined but routes use `_verify_cluster_auth()` + `_get_node_registry()` separately, NOT the `AuthenticatedNodeRegistry` wrapper | Medium | Workflow A, B | The wrapper exists (cluster_auth.py:205-285) but routes call auth verification and registry operations independently. The wrapper is unused in production routes. Not a security gap (auth still enforced), but the wrapper class is dead code. |
| RC-3 | No rate limiting on auth failures — repeated invalid token attempts are not throttled | Medium | Step A1 | Brute-force protection missing. Mitigation: HMAC-SHA256 makes guessing infeasible, but failed attempt logging should trigger alerts. |
| RC-4 | `NodeHeartbeatClient._register()` returns the node response but does NOT extract or store a new JWT token from the response | Medium | Workflow A | The client uses `auth_token` from constructor. If the server issues a new token on registration (as AuthenticatedNodeRegistry.register does), the client doesn't capture it. This is only relevant if `AuthenticatedNodeRegistry` is used server-side (currently it isn't — see RC-2). |
| RC-5 | Heartbeat identity check (`payload.user_id != node_id`) has a bypass: if `payload.user_id` is None (which the JWT allows), the check is skipped | Low | Step B2 | `cluster_auth.py:262`: `if payload.user_id and payload.user_id != node_id` — falsy user_id skips check. Issued tokens always set user_id to node_id, so this only applies to hand-crafted tokens. |
| RC-6 | `_verify_cluster_auth` silently returns if no authenticator is configured (`getattr(request.app.state, "cluster_authenticator", None)`) — deployments that forget to configure auth get no protection | Medium | All workflows | Auth is opt-in, not opt-out. If `ClusterAuthConfig` is not provided, all cluster endpoints are open. Should log a warning on startup. |
| RC-7 | `NodeInfo.id` uses `uuid.uuid4().hex[:12]` as default (models.py:1232), so the registration route in tasks.py:1371-1378 creates nodes with auto-generated unique IDs. Verified: no empty-ID collision risk. | Info | Step A1 | Confirmed correct behavior. |
| RC-8 | Registration endpoint (`register_node` at tasks.py:1357-1379) uses `_verify_cluster_auth` + `_get_node_registry` separately — it does NOT use `AuthenticatedNodeRegistry.register()` which would also issue a token in the response. Current route returns `NodeResponse` without a token field, so clients have no way to receive a server-issued session token after registration. | Medium | Step A2 | Token issuance on registration exists in `AuthenticatedNodeRegistry` (cluster_auth.py:220-238) but is unused by the routes. Clients must use a pre-shared token for the lifetime of their session. |

---

## Test Cases

| Test | Trigger | Expected behavior |
|---|---|---|
| TC-01: Happy path registration | POST /cluster/nodes with valid bootstrap token | 201 + node registered + token issued |
| TC-02: Registration without token | POST /cluster/nodes, no auth header | 401 "Missing Authorization header" |
| TC-03: Registration with invalid token | POST /cluster/nodes, Bearer invalid.token | 401 "Invalid or expired token" |
| TC-04: Registration with wrong scope | POST /cluster/nodes, token with heartbeat-only scope | 401 "Token lacks required scope" |
| TC-05: Registration with revoked token | POST /cluster/nodes, token that was revoked | 401 "Token has been revoked" |
| TC-06: Heartbeat with valid token | POST /cluster/nodes/{id}/heartbeat with matching token | 200 + node updated |
| TC-07: Heartbeat identity mismatch | Token for node-1, heartbeat for node-2 | 401 "Token node_id mismatch" |
| TC-08: Heartbeat for unknown node | Valid token but node evicted | 404 "Node not registered" |
| TC-09: Unregister without admin scope | DELETE /cluster/nodes/{id} with regular node token | 401 "lacks required scope" |
| TC-10: Unregister with admin scope | DELETE /cluster/nodes/{id} with admin token | 204 + node removed + tokens revoked |
| TC-11: Auth disabled mode | require_auth=False, no token | All operations succeed with anonymous payload |
| TC-12: Cordon/uncordon/drain require admin | POST .../cordon with node token | 401 |
| TC-13: Token refresh before expiry | Token nearing expiry | TokenRefreshScheduler issues new token |
| TC-14: Token refresh fatal | 3 consecutive refresh failures | TokenRefreshFatalError raised |

**Existing test coverage**: `tests/unit/test_cluster_auth.py` covers TC-01 through TC-12. TC-13 and TC-14 (refresh scheduler) are tested in separate JWT token tests.

---

## Assumptions

| # | Assumption | Where verified | Risk if wrong |
|---|---|---|---|
| A1 | Shared secret is the same on all nodes and the central server | Not verified at runtime | Token verification fails silently; nodes cannot register |
| A2 | Clock skew between nodes is minimal (<5 min) | Not verified | Tokens may appear expired or not-yet-valid |
| A3 | HMAC-SHA256 is sufficient for cluster-internal auth (not using RSA/EC) | Verified: symmetric key appropriate for single-cluster | If secret leaks, all tokens are compromised. mTLS would provide stronger guarantees. |
| A4 | 24-hour token TTL is acceptable for long-running worker nodes | Verified: TokenRefreshScheduler handles renewal | If refresh fails and node runs >24h, it loses auth |
| A5 | Bootstrap token distribution happens out-of-band (manual config, env var, secrets manager) | Not verified in code | No automated bootstrap enrollment workflow exists |

---

## Open Questions

- **Q1**: Should token revocation be persisted to disk or an external store to survive server restarts?
- **Q2**: Should there be a bootstrap enrollment workflow (e.g., one-time registration codes) instead of pre-shared tokens?
- **Q3**: Should the `AuthenticatedNodeRegistry` wrapper be used in routes (replacing separate auth + registry calls), or should it be removed as dead code?
- **Q4**: Should failed auth attempts be rate-limited or trigger alerts?
- **Q5**: Should the system support mTLS as an alternative/addition to JWT for node authentication?

---

## Spec vs Reality Audit Log

| Date | Finding | Action taken |
|---|---|---|
| 2026-04-08 | Initial spec created from code audit of cluster.py, cluster_auth.py, jwt_tokens.py, routes/tasks.py | — |
| 2026-04-08 | AuthenticatedNodeRegistry wrapper is dead code — routes use separate auth + registry pattern (RC-2) | Documented |
| 2026-04-08 | Revocation list is in-memory only (RC-1) | Documented as high-severity gap |
| 2026-04-08 | Auth is opt-in — no warning when unconfigured (RC-6) | Documented |
| 2026-04-08 | Verification pass: confirmed NodeInfo.id auto-generation (RC-7). Added RC-8: routes don't use AuthenticatedNodeRegistry, so no token issuance on registration — clients must use pre-shared tokens. Bumped to v1.1. | Spec updated |
