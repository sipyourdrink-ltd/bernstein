# Automation bridge

The automation bridge lets a workflow platform drive Bernstein and observe it,
with both directions cryptographically accountable. It is the shared reference
for the platform recipes:

- [n8n](n8n.md)
- [Zapier](zapier.md)
- [Workato](workato.md)

## What the platform ends up holding

| Direction | Artifact | What it proves |
| --- | --- | --- |
| Inbound | Trigger receipt | Which payload was admitted, under which scope, at which chain position |
| Inbound (refused) | Refusal receipt | That a trigger was turned away, and why |
| Outbound | Status proof | That the status the workflow acted on is the status the chain recorded |

Every one of these is signed with the install's Ed25519 identity and anchored in
the HMAC audit chain, so the platform's stored copy is checkable offline long
after the fact with a single command.

## Inbound: trigger receipts

`POST /webhook` authenticates the request exactly as before (a shared HMAC
secret plus a fresh timestamp; see [the webhook route](#request-shape)). An
admitted trigger now also returns a receipt:

```json
{
  "task": { "id": "t-42", "title": "Rotate the deploy key", "...": "..." },
  "receipt": {
    "v": 1,
    "kind": "trigger_receipt",
    "trigger_id": "n8n-exec-8172",
    "platform": "n8n",
    "request_path": "/webhook",
    "payload_digest": "sha256:...",
    "graph_digest": "sha256:...",
    "scope": "task:create",
    "outcome": "admitted",
    "refusal_reason": "",
    "admission_chain_head": "...",
    "replay_protected": true,
    "timestamp": 1700000000,
    "task_ids": [],
    "signer_public_key_pem": "-----BEGIN PUBLIC KEY-----\n...",
    "signature": "...",
    "chain_entry_hash": "..."
  }
}
```

Store the whole `receipt` object. Nothing else is needed to verify it.

### Fields worth knowing

| Field | Meaning |
| --- | --- |
| `payload_digest` | SHA-256 of the exact request body. Identical bytes always digest identically, so admission identity is reproducible. |
| `graph_digest` | Digest of the canonical task graph the payload projects. Two operators who fired the same payload carry the same value, which is how "we fired the identical graph" becomes a comparison rather than a claim. |
| `admission_chain_head` | The audit-chain head at the moment the trigger was adjudicated. |
| `replay_protected` | Whether the trigger id was a caller-supplied nonce checked against the replay ledger. See [Replay protection](#replay-protection). |
| `chain_entry_hash` | The audit-chain entry anchoring this receipt. |
| `signature` | Ed25519 signature over the canonical binding (everything above except the signature and the anchor). |

### Replay protection

Send your platform's execution id in a trigger-id header. The bridge refuses a
second trigger carrying an id it already admitted, and the refusal is itself a
signed, chain-anchored receipt returned with HTTP 409.

| Platform | Header the bridge reads |
| --- | --- |
| n8n | `X-N8n-Execution-Id` |
| Zapier | `X-Zapier-Request-Id` |
| Workato | `X-Workato-Job-Id` |
| Any | `X-Bernstein-Trigger-Id` (checked first, overrides the above) |

If you send no trigger id the bridge still mints a receipt, but it cannot tell a
captured replay apart from a legitimate re-fire of the same goal, so it does not
refuse on that basis. The receipt records `"replay_protected": false` so the
copy you store never implies a guarantee it does not carry. **Send the header.**

The refusal holds when the two deliveries arrive at once. The ledger check and
the ledger entry recording it are one section per trigger id, held against every
other thread and every other worker process, so a trigger delivered twice in
parallel is admitted exactly once and fires its task graph once. The second
delivery gets the 409 and its own anchored refusal receipt, not a second run.

### Refusals

A trigger that fails authentication (HTTP 401) or replays an admitted id (HTTP
409) still returns a `receipt`, with `outcome` set to `refused` and
`refusal_reason` naming the cause. A refused trigger is granted no scope and
projects no graph, so `scope` and `graph_digest` are empty. Refusals are chain
events in their own right: the negative path is as discoverable as the positive
one, never a silent drop.

| `refusal_reason` | Cause |
| --- | --- |
| `signature_did_not_verify` | The HMAC signature was missing or wrong |
| `timestamp_outside_replay_window` | The timestamp header was missing, malformed, or stale |
| `replayed_trigger_id` | The trigger id had already been admitted |
| `malformed_trigger_payload` | The body could not be read as a trigger |

### Refusal budget

The refusal path is reachable without the shared secret, so anchoring a signed
receipt for every bad request would let anonymous traffic grow the audit chain
at will. The bridge caps how many *unauthenticated* refusals get their own chain
entry (60 per minute by default) and counts the rest. Over the cap the trigger
is still refused, but the response carries no `receipt`, and the next anchored
refusal records a `suppressed_refusals` count so the chain never hides that they
happened.

Replay refusals are never capped: producing one requires a valid signature, so
that path is already gated by possession of the secret.

### Request shape

The endpoint requires `BERNSTEIN_WEBHOOK_SECRET` to be configured; it fails
closed with HTTP 503 when it is not. Each request carries:

- `X-Bernstein-Timestamp`: Unix seconds, within five minutes of server time.
- `X-Bernstein-Webhook-Signature-256`: `sha256=` followed by the hex HMAC-SHA256
  of `f"{timestamp}.".encode() + body` under the shared secret.

The task fields (`title`, `description`, and the rest of the task payload) sit at
the top level of the body. Each platform may additionally nest a copy under its
own envelope key (`body` for n8n, `data` for Zapier, `input` for Workato); the
bridge unwraps whichever it finds when projecting the task graph.

## Outbound: status proofs

Configure a `webhook` notification sink pointing at your platform's inbound URL:

```yaml
sinks:
  - id: workflow-callback
    kind: webhook
    url: https://example.com/hooks/bernstein
    # Optional; both shown with their defaults.
    status_proof: true
    sdd_dir: .sdd
```

The delivered body is the notification payload it always was, plus one
additional key:

```json
{
  "event_id": "evt-1",
  "kind": "post_task",
  "title": "Task t-42 finished",
  "severity": "error",
  "run_id": "run-9",
  "details": { "status": "failed" },
  "automation_bridge_proof": {
    "v": 1,
    "kind": "status_proof",
    "event_id": "evt-1",
    "run_id": "run-9",
    "status": "failed",
    "producing_event_digest": "sha256:...",
    "chain_head": "...",
    "timestamp": 1700000500,
    "signer_public_key_pem": "-----BEGIN PUBLIC KEY-----\n...",
    "signature": "...",
    "chain_entry_hash": "..."
  }
}
```

The envelope is strictly additive. Every key the plain payload carried survives
verbatim under its original name, so a consumer written before the bridge keeps
parsing the body unchanged.

Retries are byte-identical: the proof is minted once per `event_id` and cached,
so a callback re-sent after a transient delivery failure repeats the same bytes
rather than making a second, differently-anchored claim. Two callbacks in flight
for one `event_id` get the same treatment: they share one `status.proof.emitted`
chain event and are handed the same proof, which is also the proof left on disk.

Set `status_proof: false` to opt out. If the install cannot reach its audit
chain the sink logs a warning and delivers the plain payload rather than
dropping an operator's alert.

## Verifying

One command handles both artifacts. It takes the document exactly as the
platform stored it:

```bash
# A trigger receipt.
bernstein audit verify --receipt receipt.json

# Also re-digest the original request body against the receipt.
bernstein audit verify --receipt receipt.json --payload body.json

# A status callback, as received.
bernstein audit verify --receipt callback.json
```

Exit code 0 means verified; 1 means the document did not verify; 2 is a usage
error. Gate a workflow step on the exit code.

For a trigger receipt the command checks the Ed25519 signature over the binding,
that the supplied payload still digests to the recorded value, and that the
chain holds a matching anchored entry. For a status callback it additionally
checks that the status the platform was told equals the status the chain
recorded, and **reports the chain's recorded status on failure**:

```console
$ bernstein audit verify --receipt callback.json
╭──────────────────────────────────────────────────╮
│ Status Proof Verification Failed                 │
│ reported status 'succeeded' is not the status    │
│ the chain recorded                               │
│ Chain-recorded status: failed                    │
╰──────────────────────────────────────────────────╯
```

That is the answer to "the workflow says the run passed but it did not".

## Where state lives

| Path | Contents |
| --- | --- |
| `.sdd/audit/` | The HMAC audit chain every receipt anchors into |
| `.sdd/automation-bridge/triggers/` | The replay ledger, one file per admitted trigger id, plus a `.lock` file per id the bridge adjudicated |
| `.sdd/automation-bridge/status/` | Minted status proofs, cached so retries repeat, plus a `.lock` file per event id |
| `.sdd/automation-bridge/automation-bridge-identity-key.pem` | The install's Ed25519 signing key, mode `0600` |

Set `BERNSTEIN_AUTOMATION_BRIDGE_ROOT` to relocate the bridge state directory.
Back up the identity key with the rest of your install secrets: losing it does
not invalidate already-issued receipts (each embeds its public key) but new
receipts will be signed under a fresh identity.

## MCP

Platforms that speak MCP reach the same bridge through the existing MCP server
surface; there is no separate protocol and no per-platform SDK.
