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

The streamable HTTP transport is served by a separate implementation
(`bernstein.mcp.remote_transport`) from the stdio and SSE transports
(`bernstein.mcp.server`). It exposes 8 tools rather than the full tier
catalogue, and it enforces weaker argument validation than stdio; see
[Validation scope](input-validation.md#validation-scope) and
[Available tools](../cloudflare/cloudflare-mcp.md#available-tools). Unifying
the two is tracked in issue #3083.

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

Every `401` the streamable HTTP transport returns while an issuer is
configured carries a challenge naming that document, so a client that is
refused can locate it without knowing the well-known path in advance:

```
WWW-Authenticate: Bearer resource_metadata="https://bernstein.example.com/.well-known/oauth-protected-resource"
```

The URL is built from the same request base (`Host` plus
`X-Forwarded-Proto`) as the `resource` field inside the document, so the
advertised URL and the served path cannot drift. The value names nothing
but that URL: no token, tenant, user, or issuer identifier appears in it.
When no issuer is configured the header is omitted entirely and the
anonymous and static-bearer flows behave exactly as before. The `401` body
is `{"error":"unauthorized"}` in both cases.

The discovery handshake is:

0. Client calls `/mcp` without credentials, is refused with `401`, and
   reads the metadata URL from `WWW-Authenticate`.
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

## Connect-time instructions

The server sends an `instructions` string on connect. It is the only
Bernstein text guaranteed to stay in a connected model's context for the
whole session, so it carries the control loop rather than a description of
the system:

1. One clause of identity.
2. The start-then-poll loop: `bernstein_run` returns a `task_id` which is the
   run id, `bernstein_task_handle` is polled with that value as `run_id`,
   runs take minutes to hours, poll tens of seconds apart, and stop at a
   terminal status (`completed`, `failed`, `cancelled`).
3. One pointer to `load_skill` for anything deeper.

Two rules keep the text honest, both enforced in
`tests/unit/test_mcp_server.py`:

- The string stays at or under 900 characters.
- Every tool name it mentions is registered on the server, so instruction
  text cannot outlive a tool rename.

The `src/bernstein/mcp/server.py` module docstring lists every tool the
module registers, and the same test asserts that list against the live
registration set.

## Driving long-running runs from an MCP host (Tasks extension)

A run started over MCP can outlive a single call. Rather than hold a session
open for the whole run, a host drives it with a **verifiable run handle** it
polls (MCP Tasks extension, pinned revision `2026-07-28`). The handle is not
free-standing server state: its status is a pure projection of the run
journal, and it embeds the run's audit-chain head so the host can later prove
the task it watched corresponds to the audited run.

1. Start a run with `bernstein_run`. The response body carries everything the
   poll loop needs:

   ```json
   {
     "task_id": "abc123",
     "title": "Add auth",
     "status": "open",
     "run_id": "task-abc123",
     "poll_after_ms": 5000
   }
   ```

   `task_id` names the task on the task server; `run_id` names its run
   journal (the task id slugified, see `task_run_id`). `poll_after_ms` is the
   advisory delay before the first poll. A run takes minutes to hours, so a
   host waits and polls rather than re-issuing `bernstein_run`.

2. Poll `bernstein_task_handle` with **either** identifier from that body.
   The tool resolves the journal run id first and the slugified task id
   second, so both forms reach one journal and project an identical handle.
   It reprojects the handle from the on-disk run journal and the audit-chain
   head, so any server instance answers identically and the host holds no
   session:

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

### Task-augmented calls (native task handles)

A host that implements the Tasks extension can skip the polling fallback and
ask for a native task row instead.

- `bernstein_run` advertises `execution.taskSupport: "optional"` in its
  `tools/list` entry, so a host may invoke it either way. A **plain**
  `tools/call` returns the usual `CallToolResult` with the run JSON. A
  **task-augmented** `tools/call` (one that carries `task` in the request
  params) returns a `CreateTaskResult` whose `task.taskId` the host then
  drives with `tasks/get`, `tasks/result`, `tasks/list`, and `tasks/cancel`.
  The response shape follows the call, not the host's declared capability: a
  tasks-capable host that sends a plain call still gets a `CallToolResult`.
- `bernstein_task_handle` advertises `execution.taskSupport: "forbidden"`. It
  is the stateless polling fallback and always answers immediately, so it must
  not be invoked as a task.
- Every task row carries a finite `ttl` (24h) rather than the `null` that
  spells "unlimited". A `null` ttl is dropped from the serialised response and
  the receiving host rejects the row, so the retention window is stated
  explicitly. It under-claims: nothing evicts the run.

Hosts with no Tasks support are unaffected and keep using
`bernstein_task_handle`.

### Connecting a host trace to the run's artefacts

W3C Trace Context arriving in a request `_meta` (`traceparent` / `tracestate`
/ `baggage`) is ingested and recorded into the lineage of the artefacts the
run produces, so a trace from the calling host connects to the run's outputs.
The ingested `traceparent` is carried as the lineage entry's `step_id`
cross-link; a verifier holding the lineage spine reads the host trace off the
artefact's provenance row.

## Stopping a run: which project `bernstein_stop` can reach

`bernstein_stop` takes a `workdir` and writes
`<workdir>/.sdd/runtime/signals/SHUTDOWN`, which the orchestrator picks up
and drains on. The `workdir` arrives from the caller, so the tool applies the
same containment barrier the run-journal readers use before it touches the
filesystem:

- The workdir is screened for shape first, with no filesystem call: a value
  that is not text, or that is longer than a path may be on this filesystem
  (`MAX_PATH_BYTES`), cannot address a directory and is refused up front.
- The workdir is resolved, and the signal path is rebuilt through the shared
  containment helper. A `.sdd`, `runtime`, `signals`, or `SHUTDOWN` entry that
  is a symlink pointing out of the resolved root is refused, because the write
  would land outside the project the caller named.
- The resolved root must already contain a `.sdd` directory. The tool stops a
  Bernstein project that exists; it does not create a project tree at a path it
  is handed.
- A refused call creates no directory and writes no file. It returns the
  structured tool error (`error` plus `hint`), never a `status`.

Naming a root is allowed. An absolute path to a second Bernstein project on
the same machine is a legitimate stop target; the barrier is against a
`workdir` that reaches past the root it names. Both the stdio server and the
remote HTTP transport serve the tool through the same helper, so the two
surfaces cannot drift apart.

Resolving the path is not the barrier on its own: `resolve()` follows symlinks
but does not fold case, so on a case-insensitive filesystem a resolved path is
normalised rather than canonical. Containment against the root as resolved in
that same call is what decides whether the write stays inside.

Resolving is also not free. It stats one entry per path component, and the
remote HTTP transport serves this tool straight from the JSON-RPC arguments
with no tool schema in front of it, so the caller picks the length. That is
why the shape screen runs first: an over-long `workdir` is refused on its byte
count rather than walked component by component on the serving event loop.
Every refusal, including one from an input the filesystem cannot represent
such as an embedded NUL, comes back as the same structured tool error rather
than a raw filesystem message.

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

3. **Complete** with `bernstein_complete`, passing the summary of what the
   work produced. This is the worker's completion verb. `bernstein_approve`
   is not: it grants an approval a task is waiting on and refuses a task the
   worker is executing (see below).

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

## The approval gate

`bernstein_approve` signs off a finished result that is waiting on a decision.
It is not a way to finish work: it reads the task first and acts only on the
state the task lifecycle holds a completed result in, taken from
`bernstein.core.tasks.lifecycle.APPROVABLE_TASK_STATUSES`.

| Current status | What approval means | Endpoint | Resulting status |
|---|---|---|---|
| `pending_approval` | Sign off finished work, recording the approver's note as the result summary. | `POST /tasks/{id}/complete` | `done` |

Any other status is refused with a structured error naming the current status,
and no state-changing request is sent:

```json
{
  "error": "task_not_awaiting_approval",
  "task_id": "t-42",
  "current_status": "in_progress",
  "approvable_statuses": ["pending_approval"],
  "message": "Task t-42 is in status 'in_progress'. bernstein_approve only acts on a task holding a finished result for sign-off (pending_approval), and never forces another state forward.",
  "hint": "To finish work you are executing, use bernstein_complete. To report that the task is stuck, post to the task mailbox with bernstein_update. To abandon the work, cancel the task (bernstein task cancel <task_id>)."
}
```

A task that is stuck, blocked, or unfinished has no approval to grant. Finish
work you are executing with `bernstein_complete`, report a blocker with
`bernstein_update`, or abandon the work with `bernstein task cancel <task_id>`.

### `planned` is decided on the plan, not on the task

A task in `planned` is held by plan mode, and its decision is recorded on the
plan that holds it: `POST /plans/{plan_id}/approve` records the operator
decision and promotes every task the plan covers. `bernstein_approve` refuses
`planned` and says so, because releasing one task at a time would start the
work while the plan is still `pending` - and `POST /plans/{plan_id}/reject`
only cancels tasks that are still `planned`, so a task released early survives
a rejection of the plan it belongs to.

## The completion gate

`bernstein_complete` reports the result of work the caller is executing. It
reads the task first and posts only from a state a worker holds the task in,
taken from `bernstein.core.tasks.lifecycle.WORKER_COMPLETABLE_TASK_STATUSES`:
`open` (the MCP claim path claims from the shared backlog and the completion
route re-claims the task before applying the payload), `claimed`, and
`in_progress`.

Three states with a legal transition to `done` are deliberately excluded, so
that the completion verb cannot be used to clear a task out of the way:

| Refused status | Why | What to do instead |
|---|---|---|
| `waiting_for_subtasks` | The parent is completed by its last subtask finishing. Completing it directly marks the parent done while the subtasks are still running. | Let the subtasks finish, or cancel them. |
| `orphaned` | The worker is gone, so no caller is executing the task. | Crash recovery decides; re-queue it. |
| `pending_approval` | The result is already recorded and waiting on a decision. | `bernstein_approve`. |

The refusal names the current status:

```json
{
  "error": "task_not_completable",
  "task_id": "t-42",
  "current_status": "waiting_for_subtasks",
  "completable_statuses": ["claimed", "in_progress", "open"],
  "message": "Task t-42 is in status 'waiting_for_subtasks'. bernstein_complete reports the result of work you are executing (claimed, in_progress, open), and does not finish a task that is waiting on its subtasks, whose worker is gone, or whose result is already awaiting a decision."
}
```

Both gates are enforced identically on the stdio server and on the streamable
HTTP transport, from `bernstein.mcp.approval_gate`, so a caller cannot pick a
transport to get the weaker rule.

The gate does not establish *which* worker is calling: the task server accepts
a completion from any caller holding `tasks:write`, and the claim identity the
MCP claim path records lives in the shared backlog file rather than on the
task. A caller with write access can therefore still complete a task another
worker is executing. Scope the credential per task (the agent identity JWT
carries a `task_ids` claim that the server enforces on `POST /tasks/{id}/complete`)
when that matters.

### What the gates do not cover

Both gates read the task and then write, and the two requests cannot be made
atomic from the client: `POST /tasks/{id}/complete` takes no expected-state
precondition. The gate therefore decides *which* endpoint is called, and the
task server decides whether the call lands.

Where the state machine has no edge to `done` the write is rejected, so a task
that moves into `blocked` or `cancelled` after the read is not completed. Where
it has one, the write lands on whatever the task became: a task that enters
`waiting_for_subtasks` between the read and the write is completed even though
a direct call in that state is refused. Closing that needs a precondition on
the completion route rather than a second client-side check.

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

   # or start from the refusal and follow the challenge:
   curl -si http://127.0.0.1:8053/mcp -d '{}' | grep -i www-authenticate
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
