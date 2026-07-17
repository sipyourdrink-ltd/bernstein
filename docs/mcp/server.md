# Bernstein MCP server

Bernstein exposes its orchestration layer as MCP tools so any MCP client
(Cursor, Claude Code, Cline, Windsurf, and others) can drive multi-agent work
through Bernstein. This page describes the protocol surface and how to point a
client at the server. For the per-tier tool catalogue see
[`tool_tiers.md`](tool_tiers.md).

## Transports

| Transport | Command | Use when |
|-----------|---------|----------|
| stdio (default) | `bernstein mcp` | Local IDE integration. |
| SSE | `bernstein mcp --transport http` | Remote/web integration. |
| Streamable HTTP | served on `/mcp` | Stateless remote integration with cancellation. |

The streamable HTTP transport binds to loopback by default. Binding to a
public interface requires a bearer token (see Auth) and is otherwise refused
at startup.

## Stateless serving

The stateless MCP spec revision (2026-07-28) removes protocol sessions, and
the transports removed them with it: no server-side session store exists.
Every request is served from its body plus the per-request `_meta` alone, so
consecutive requests may land on different transport instances with no shared
memory and produce identical results. The SSE gateway correlates each
response to its request by the content-derived span id (the
`X-Bernstein-Span-Id` response header and the SSE event `id` line) instead of
per-session queues.

Cross-call continuity is anchored in the run journal and the audit chain
instead of a session: when a run journal is wired in, every served or proxied
`tools/call` becomes an ordered `mcp.stateless_call` journal row and (with an
audit chain) a chain entry binding the call's content-derived trace and span
ids to the journal head. `bernstein audit verify` reconstructs the full MCP
call ordering of a run purely from those chain entries; tampering with any
single entry fails verification at exactly that entry.

Legacy clients that still send the removed `Mcp-Session-Id` header keep
working during a bounded compatibility window: the header is accepted and
ignored (never stored, never echoed back), and a legacy `DELETE`
session-close request is acknowledged as a no-op. The window closes twelve
months after the deprecating spec revision (2027-07-28); after that the
header and the `DELETE` lifecycle are refused with an error naming the
removal date.

## Auth

| Mode | How | Notes |
|------|-----|-------|
| Anonymous | default on loopback | Allowed only on `127.0.0.1` / `localhost` / `::1`. |
| Static bearer | `BERNSTEIN_MCP_TOKEN` (or `BERNSTEIN_MCP_AUTH_TOKEN`) | Constant-time check; required on non-loopback binds. |

OAuth-2 PKCE token issuance is delegated to an external IdP. Bernstein is
the **resource server**: it does not host an authorization server and so
does not publish RFC 8414 authorization-server metadata. When the operator
sets `BERNSTEIN_MCP_OAUTH_ISSUER=https://idp.example.com`, the streamable
HTTP transport serves a single discovery document so a host can locate
the IdP:

| Path | Document |
|------|----------|
| `/.well-known/oauth-protected-resource` | RFC 9728 / MCP-draft protected-resource metadata pointing at the issuer; the `resource` field is built from the request `Host` and `X-Forwarded-Proto` headers. |

The discovery handshake is:

1. Client fetches `/.well-known/oauth-protected-resource` from Bernstein.
2. Client reads `authorization_servers[0]` (the configured issuer URL).
3. Client fetches the IdP's own RFC 8414 metadata from the IdP, for
   example `https://idp.example.com/.well-known/oauth-authorization-server`
   or whatever path the IdP uses (Keycloak, Auth0, Okta all differ).
4. Client completes the PKCE S256 authorization-code flow against the
   IdP and presents the resulting bearer token to the streamable HTTP
   transport.

The protected-resource path is served without authentication, since a
client probing discovery has no token yet. When the env var is unset,
the path returns 404 and only anonymous (loopback) / static bearer are
advertised. Bernstein never serves `/.well-known/oauth-authorization-server`;
that document belongs to the IdP, not the resource server.

`BERNSTEIN_MCP_OAUTH_SCOPES` (comma-separated) overrides the default
`bernstein.read,bernstein.write` scope list in the document.

The capability card reports the discovery state under `auth.oauth` so a
client that has already fetched the card can locate the well-known path
without probing. OIDC federation is still a follow-up.

## Capability cards

Beyond the static `capabilities` object on `initialize`, the server publishes
a runtime capability card describing how it is actually running: reachable
transports, configured auth modes, the active tool tier, the cost-meter
state, and the targeted spec revision. The card is built from live process
state on each read, so it reflects the current configuration without a
restart.

The card is available two ways:

- as the `bernstein://capability` MCP resource (read it with the client's
  resource API);
- under the `capabilityCard` key on the streamable HTTP transport's
  `initialize` result.

## Built-in prompt catalogue

The server ships three orchestration-focused prompt templates exposed via
the MCP `prompts/list` and `prompts/get` routes. A host that auto-discovers
MCP servers can populate a prompt picker without sending a tool call first.

| Prompt | Arguments | Use when |
|--------|-----------|----------|
| `orchestrate_goal` | `goal` (required), `role`, `scope` | Planning a single Bernstein run from a free-form goal. |
| `triage_failed_tasks` | `limit` (default 5) | Reviewing recent failed tasks and proposing next actions. |
| `cost_recap` | `window` (default `today`) | Summarising cost-per-role across a labelled window. |

Each prompt renders deterministically from its arguments and does not call
the task server. The capability card lists the catalogue under
`prompts.catalogue` so a client that has already fetched the card can pick a
prompt without a second probe.

## Per-call cost-meter envelope

Every tool response is wrapped in a uniform envelope so observability is
consistent across transports:

```json
{
  "result": { "status": "ok" },
  "_meter": {
    "tool": "bernstein_health",
    "call_id": "b1c2...",
    "latency_ms": 12.4,
    "cost_usd": 0.0,
    "ok": true,
    "ts": "2026-05-20T10:11:12.345Z"
  }
}
```

`cost_usd` is best-effort: the MCP server proxies to the task server and does
not itself spend model tokens, so the per-call figure is `0.0` unless a
handler attaches a cost. The field exists so the envelope shape is stable.

To get the bare tool payload (the historical shape), disable the meter:

```bash
export BERNSTEIN_MCP_COST_METER=0
```

## Streaming cancel with partial-result preservation

On the streamable HTTP transport, each `tools/call` runs as a cancellable
task tracked by its JSON-RPC id. A client cancels an in-flight call by sending
a `notifications/cancelled` notification carrying that `requestId`. The
originating call then returns the work done before the stop rather than a bare
error:

```json
{
  "content": [{ "type": "text", "text": "{\"status\": \"running\", ...}" }],
  "cancelled": true,
  "partial": ["{\"status\": \"running\", \"tool\": \"bernstein_run\"}"],
  "_meter": { "tool": "bernstein_run", "ok": false, "...": "..." }
}
```

`isError` is not set: a cancel is a client-initiated stop, not a tool failure.
Cancelling an unknown or already-settled id is a no-op.

## Driving long-running runs from an MCP host (Tasks extension)

A run started over MCP can outlive a single call. Rather than hold a session
open for the whole run, a host drives it with a **verifiable run handle** it
polls (MCP Tasks extension, pinned revision `2026-07-28`). The handle is not
free-standing server state: its status is a pure projection of the run
journal, and it embeds the run's audit-chain head so the host can later prove
the task it watched corresponds to the audited run.

1. Start a run with `bernstein_run`; note the returned `task_id` (the run id).
2. Poll `bernstein_task_handle` with that run id. The tool reprojects the
   handle from the on-disk run journal and the audit-chain head, so any server
   instance answers identically and the host holds no session:

   ```json
   {
     "taskId": "run-2364",
     "runId": "run-2364",
     "status": "completed",
     "journalHead": "<merkle head of the run journal>",
     "chainHead": "<audit-chain head embedded at projection time>",
     "specRevision": "2026-07-28",
     "receiptHash": "<content-addressed digest of this handle>",
     "pollToken": "<opaque base64; carries only the run identity>"
   }
   ```

   `status` is one of `working`, `input_required`, `completed`, `failed`,
   `cancelled`. A host without Tasks support polls on an interval (the polling
   fallback); a host with Tasks support reads the same fields from the task.

3. When the run reaches a terminal status, the handle's embedded `chainHead`
   verifies against the completed run's audit chain with `bernstein audit
   verify` (or the offline verifier
   `bernstein.core.protocols.mcp.tasks_extension.verify_handle_chain_head`).
   Because `receiptHash` is a deterministic digest over the projected status,
   the journal head, and the chain head, a forged progress claim fails
   verification: the handle *is* the proof, not a view onto it.

### Connecting a host trace to the run's artefacts

W3C Trace Context arriving in a request `_meta` (`traceparent` / `tracestate`
/ `baggage`) is ingested and recorded into the lineage of the artefacts the
run produces, so a trace from the calling host connects to the run's outputs.
The ingested `traceparent` is carried as the lineage entry's `step_id`
cross-link; a verifier holding the lineage spine reads the host trace off the
artefact's provenance row.

## Running the pull-worker loop over MCP

An MCP-native worker drives its own claim, update, complete loop over MCP
alone, with no second integration path (no CLI, no raw HTTP). Every step
returns an object that verifies offline against the same audit chain
`bernstein audit verify` walks: strip the chain and the signatures and the
loop loses its meaning, not merely its log.

1. **Claim** with `bernstein_claim`. The tool drives the dependency-gated
   claim path: a task is offered only when every id in its `depends_on` is
   present in `completed_ids`. Instead of a mutable task projection it returns
   a signed **claim receipt** the worker holds:

   ```json
   {
     "taskId": "t-42",
     "granted": true,
     "claimerCardFingerprint": "sha256:<claimer card>",
     "backlogHead": "sha256:<digest of the backlog snapshot>",
     "filterDigest": "sha256:<digest of the claim filter>",
     "chainHead": "<audit-chain head the claim event recorded>",
     "specRevision": "2026-07-28",
     "receiptHash": "<content-addressed digest of this receipt>",
     "signature": "<Ed25519 signature over the receipt hash>",
     "signerPublicKeyPem": "<PEM public half>",
     "pollToken": "<opaque base64; carries only the receipt identity>"
   }
   ```

   A filter that matches no eligible task returns a signed **refusal receipt**
   (`"granted": false`, empty `taskId`) - a claim attempt is never a silent
   skip. Wall-clock is excluded from the pre-image, so replaying the same
   backlog snapshot, claimer, and filter produces a byte-identical
   `receiptHash`.

2. **Report progress** with `bernstein_update`, as many times as needed. Each
   update is DLP-redacted, HMAC-chained onto the worker mailbox journal,
   Ed25519-signed, and mirrored to the audit chain (`task.mailbox_message`)
   before returning. The result IS the signed journal entry (`seq`,
   `prev_entry_hash`, `entry_hash`, `signature`, `body_hash`), not a bare
   status string.

3. **Complete** with `bernstein_approve` (the existing completion verb).

Every claim and every update appears as an audit-chain entry
(`task.claim_receipt` and `task.mailbox_message`), and no new audit event type
is introduced. The claim receipt verifies offline - no network, no running
server - by reprojecting the backlog head from the on-disk backlog and
checking the embedded chain head:

```bash
bernstein backlog verify-claim --receipt receipt.json \
  --backlog .sdd/runtime/task-backlog.json --audit-dir .sdd/audit
```

The offline verifier
`bernstein.core.protocols.mcp.claim_receipt.verify_claim_receipt` performs the
same check in-process. Because ownership disputes and progress questions are
settled by replay against the chain rather than by trust, the task lifecycle
surface over MCP is complete and uniformly provable: create, query, claim,
update, complete, cancel, each returning an artifact that verifies against the
chain.

## Worked example: pointing a host at the server

1. Start the server over the streamable HTTP transport on loopback:

   ```bash
   export BERNSTEIN_MCP_TOKEN=dev-token
   bernstein mcp --transport http --host 127.0.0.1 --port 8053
   ```

2. Initialize and read the capability card:

   ```bash
   curl -s http://127.0.0.1:8053/mcp \
     -H "Authorization: Bearer dev-token" \
     -H "content-type: application/json" \
     -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"clientInfo":{"name":"example"}}}'
   ```

   The `result.capabilityCard` shows the active tier, auth modes, and
   transports the client can use.

3. Call a tool. The response carries the cost-meter envelope:

   ```bash
   curl -s http://127.0.0.1:8053/mcp \
     -H "Authorization: Bearer dev-token" \
     -H "content-type: application/json" \
     -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"bernstein_status","arguments":{}}}'
   ```

4. List and fetch a built-in prompt:

   ```bash
   curl -s http://127.0.0.1:8053/mcp \
     -H "Authorization: Bearer dev-token" \
     -H "content-type: application/json" \
     -d '{"jsonrpc":"2.0","id":3,"method":"prompts/list"}'

   curl -s http://127.0.0.1:8053/mcp \
     -H "Authorization: Bearer dev-token" \
     -H "content-type: application/json" \
     -d '{"jsonrpc":"2.0","id":4,"method":"prompts/get","params":{"name":"orchestrate_goal","arguments":{"goal":"ship X","role":"qa"}}}'
   ```

5. (Optional) Probe OAuth-2 discovery before authenticating:

   ```bash
   export BERNSTEIN_MCP_OAUTH_ISSUER=https://idp.example.com
   # restart the server to pick up the env var, then:
   curl -s http://127.0.0.1:8053/.well-known/oauth-protected-resource
   ```

   The protected-resource document points at the IdP via
   `authorization_servers[0]`. The client then fetches the IdP's own RFC
   8414 metadata from the IdP (its path is IdP-specific) and completes
   the PKCE flow there, presenting the resulting bearer token to the
   streamable HTTP transport.

6. Cancel a long-running call by its id (in a second request, while the call
   is in flight):

   ```bash
   curl -s http://127.0.0.1:8053/mcp \
     -H "Authorization: Bearer dev-token" \
     -H "content-type: application/json" \
     -d '{"jsonrpc":"2.0","method":"notifications/cancelled","params":{"requestId":2}}'
   ```

   The original call returns `cancelled: true` with its preserved `partial`
   output.
