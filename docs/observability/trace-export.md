# Trust records (trace export)

`bernstein trace export` emits a **TRACE 0.2 Trust Record** — a signed
JWT-style JSON blob (Ed25519) that proves a run's journal chain is intact
and binds the run to the install identity. It is generated entirely
offline from local state; no OTLP endpoint or network is required.

TRACE is an open specification hosted at the Linux Foundation. Bernstein
implements it as an independent Apache-2.0 project; its author contributes
to the specification as an outside contributor, and Bernstein is not
affiliated with or endorsed by the specification's maintainers or the
Foundation.

```
bernstein trace export <RUN_ID> [--out PATH] [--out-dir DIR] [--json] [--last] [--sdd-dir PATH]
```

- `--last` picks the most recently finished run in `.sdd/runs/`
  (a directory with a non-empty `journal.jsonl`), sorted by mtime.
- `--out` writes the canonical JSON string to a file instead of stdout.
- `--out-dir` writes one record per worker hop (`<exec_id>.json`) plus the
  run-level `aggregate.json` into a directory.
- `--json` emits the canonical JSON form (identical to the default output).
- `--sdd-dir` overrides the `.sdd/` path; defaults to `./.sdd/` or `./.`.

Exit codes: `0` = exported, `1` = run not found / chain broken / emit
error, `2` = missing `RUN_ID` argument.

The trace extra (`bernstein[trace]`) is required.

## Multi-worker runs

A run that spawned more than one worker (more than one `agent_spawned`
event in the journal) exports one Trust Record per worker hop plus a
run-level aggregate:

- `exec_id` of a hop record is that spawn event's `agent_id`; hops appear
  in spawn order.
- each hop's `model` comes from its own `agent_spawned` event, never
  borrowed from a sibling; a hop whose spawn carries no
  `model_provider`/`model_id` is refused and the agent id is named.
- `policy.bundle_hash` is the run-level `gate_config`, identical across
  hops.
- `tool_transcript` is present only when the journal holds `tool_call`
  events for that hop; an unobserved transcript is omitted. The aggregate
  carries `tool_transcript` only when every member record carries one.
- the aggregate's `references[]` list content-binds each hop record.

With `--out-dir DIR` the CLI writes `DIR/<exec_id>.json` per hop and
`DIR/aggregate.json`. Without `--out-dir`, a multi-worker run writes only
the aggregate to `--out`/stdout and prints a note on stderr that the
per-hop records require `--out-dir`.

Each file (hop record or aggregate) verifies the same way as a
single-record export:

```
trace-tests verify --record DIR/<exec_id>.json --level 0
```

A run with no `agent_spawned` event (legacy journal) keeps the
single-record behaviour: one record, `exec_id` equal to the run id, and
`tool_transcript` always present.

## What is in a trust record

Every trust record carries a fixed set of claims. The data class is
`TrustRecordEmitter` (`src/bernstein/core/observability/trust_record.py`);
see its docstring for the canonical shape.

| Field | Value |
|---|---|
| `eat_profile` | `tag:agentrust-io.com,2026:trace-v0.2` — identifies this as a TRACE v0.2 Trust Record. |
| `iat` | Execution completion time, Unix epoch seconds, sourced from the journal. |
| `subject` | SPIFFE URI scoped to the execution: `spiffe://bernstein.run/run/<run>/exec/<exec>`. |
| `model` | `{"provider": ..., "model_id": ..., "version"?}`. |
| `runtime` | `{"platform": "software-only", "measurement": "sha256:0000…"}` — software evidence only; never a real hardware measurement. The all-zero digest is the honest way to say "no hardware measurement exists". |
| `policy` | `{"bundle_hash": <sha256>, "enforcement_mode": "enforce"}`. |
| `data_class` | Operator-declared data sensitivity; defaults to `confidential` when undeclared. |
| `data_class` | Operator-declared data sensitivity. Allowed values: `restricted`, `internal`, `confidential`, `public`. Defaults to `confidential` when undeclared. Set in `bernstein.yaml` via `data_class: <value>`. |
| `tool_transcript` | `{"hash": <sha256>, "call_count": <int>}` — hash over tool-call entries in the journal. |
| `build_provenance` | `{"slsa_level": 0, "digest": <sha256>, "provenance_uri": <release page URL>}`. |
| `appraisal` | `{"status": "none", "verifier": "https://bernstein.run/trace/verifier", "timestamp": <int>}`. |
| `cnf` | `{"jwk": {"kty": "OKP", "crv": "Ed25519", "x": <base64url>, "kid": <key-id>}}` — the public Ed25519 key for the install identity that produced `signature`. `kid` names that key. |
| `delegation` | Present only on delegated child hops: `{"parent_record_hash": <sha256>, "credential_id": <str>}`. Absent (not null) on root/solo executions. |
| `references` | Produced-artifact pointers (`rel: "produced-artifact"`) when the execution produced any; absent (not an empty list) otherwise. |
| `signature` | Base64url (no padding) Ed25519 signature over the JCS canonicalisation of every other field. |

The signed body is the JCS canonical JSON form of all fields except
`signature` — optional members (`delegation`, `references`) are omitted
entirely when absent, never emitted as `null`. RFC 8785 canonicalisation
treats "key present" and "key absent" as different bytes.

### Where each field is read from

The emitter reads a run journal written by the orchestrator. The mapping
from record member to the journal event and key it is sourced from:

| Record member | Journal event | Journal key |
|---|---|---|
| `model.provider` | `agent_spawned` | `model_provider`, else the namespace prefix of `model_id` |
| `model.model_id` | `agent_spawned` | `model_id` |
| `model.version` | `agent_spawned` | `model_version` (optional) |
| `policy.bundle_hash` | `run_started` | `gate_config` |
| `data_class` | any event | `data_class` (optional) |
| `tool_transcript` | `tool_call` | payload |
| `iat` / `appraisal.timestamp` | last event | `ts` |

When a session resolves no provider, a namespaced model identifier such as
`omnilab/fleet-hard` journals `model_provider` as its namespace (`omnilab`)
and keeps `model_id` whole. A bare identifier or an empty namespace is not a
provider, so export still refuses rather than inventing a vendor name.

`model_provider` is the model vendor the adapter declares (for example
`anthropic` for a Claude Code worker), not the CLI adapter identifier that
carried the spawn. An adapter that fronts several vendors, a gateway, or
nothing it can name declares no vendor; its hop journals no
`model_provider` key and export refuses it with the agent id, the same way
it refuses an endpoint-routed worker.

`model_id` is the identifier the operator configured, recorded as written. A
role policy that asks for a tier - `sonnet`, `opus`, `haiku` - records that
word, because that is what was asked for; the concrete dated identifier the
adapter launched is not journaled. Pin a model in the role policy when the
record has to name the exact model that ran, as it does for a record that
leaves this install.

## What is deliberately NOT in a trust record

Trust records are **provenance envelopes**, not data containers. They
prove *about* a run; they do not carry the run's payload.

- **Prompt text**. Input prompts are redacted before they enter the
  journal (see [trace store — credential safety](trace-store.md#credential-safety)),
  and they are never part of the trust record either.
- **Artifact contents**. Produced-file bytes, model responses, and other
  large payloads are not embedded. A `references` entry names an artifact
  (`id`) and carries its content digest (`digest`), but the bytes
  themselves live elsewhere (the content-addressed trace store, the
  run directory).
- **Raw journal rows**. The record hashes the tool transcript but does
  not include individual journal events. Verifiers that need the full
  chain read the journal directly from `.sdd/runs/<run_id>/journal.jsonl`
  and verify the chain independently.
- **Private keys**. The `cnf.jwk` field carries only the **public**
  half. The private signing key never leaves the install keyring.

If a verifier needs prompt text or artifact contents, it must read them
from the journal and the content-addressed store — the trust record
itself is intentionally lightweight and does not replicate them.

## Verifying offline

The record is self-contained: anyone with the install public key (from
`cnf.jwk`) can verify the signature without network access. For schema
and semantics conformance, the reference executable suite
`agentrust-trace-tests` is available on PyPI.

Install it (or resolve it via `uv run --with`):

```bash
pip install agentrust-trace-tests
```

Then verify a record file against the TRACE v0.2 schema:

```bash
trace-tests verify --record <file> --level 0
```

Level 0 validates the schema, the SPIFFE subject, the `cnf.jwk` key type,
the Ed25519 signature, the policy and runtime digests, and the build
provenance — eight checks. Every record this exporter writes passes it.

**Level 1 is not reachable from a software-only install, by design.** It
adds two requirements that are properties of the deployment rather than of
the record: `runtime.platform` must name a hardware TEE (`software-only`
is rejected as development-mode), and the verifier must supply its own
expected nonce. Running `--level 1` against a software-only record fails
`TR-RTE-001` and `TR-RTE-004` while every other check passes. Claim Level 0
for a software-only deployment; Level 1 describes a record produced inside
a TEE and checked by a verifier that issued the nonce.

### Vectors for testing

Seven committed test vectors under
`tests/fixtures/trust-record-vectors/` cover the record shapes:

- `single-execution-trust-record.json` — root, non-delegated execution
- `delegated-parent-trust-record.json` — parent hop of a delegated run
- `delegated-child-trust-record.json` — child hop, carries `delegation`
- `delegated-grandchild-trust-record.json` — third hop, two links deep
- `aggregate-trust-record.json` — run-level rollup, carries
  `references[rel=member-execution]`
- `supplementary-plane-parent-trust-record.json` and
  `supplementary-plane-child-trust-record.json` — a pair whose parent
  `cnf.jwk` carries a key outside the Basic Multilingual Plane, so the
  child's link resolves only under RFC 8785's UTF-16 key order and not
  under a code-point sort (see the fixtures' README)

Run the reference suite against any one:

```bash
uv run --with agentrust-trace-tests==0.5.1 trace-tests verify \
    --record tests/fixtures/trust-record-vectors/single-execution-trust-record.json \
    --level 0 --max-age 999999999999
```

(`--max-age` is set far above the default 24 h window because the
fixture vectors use a frozen 2023-11-14 clock, not wall-clock time —
an unmodified default would reject every vector as stale.)

## Identifier URIs {#trace-identifiers}

Two fixed URIs appear in every trust record this producer emits. Both
resolve to this section.

### `https://bernstein.run/trace/verifier` {#trace-verifier}

The `appraisal.verifier` value. It names the appraisal method, not the
workload: this producer always self-declares `status: "none"`, so the
record carries no third-party appraisal. A verifier that needs a real
appraisal must perform one itself.

### `https://bernstein.run/trace/records` {#trace-records}

The resolver named on an aggregate record's
`references[rel=member-execution]` entries. It identifies the party
obliged to resolve a member's `id` back to the record it names, which is
always this producer. The entry's `digest` binds
that id to specific bytes, so a verifier checks the member record it was
given against the digest rather than fetching it from this URL.

## Relationship to other trace commands

| Command | What it emits / checks |
|---|---|
| `bernstein trace export <RUN_ID>` | Signed TRACE 0.2 Trust Record (this page) |
| `bernstein trace project <RUN_ID>` | Signed OTel GenAI span set projected from the journal |
| `bernstein trace verify-projection <RUN_ID>` | Verifies the OTel span projection against the journal |
| `bernstein trace show <task-id>` | Pretty-printed live JSONL trace for a task |
| `bernstein trace serve` | Read-only FastAPI viewer over the local content-addressed trace store |

The trust record and the OTel projection are independent outputs from
the same journal. The trust record proves provenance; the projection is
a structured telemetry view. See [OTel span projection (offline)](otel-span-projection.md)
for the span-side workflow.

## Source

`src/bernstein/cli/commands/advanced_cmd.py` (`trace export`),
`src/bernstein/core/observability/trust_record.py`
(`TrustRecordEmitter`, `sign_trust_record`, `verify_trust_record`),
`tests/fixtures/trust-record-vectors/` (committed test vectors),
`schemas/trace-spec/0.2/trace-v0.2.json` (vendored schema).
