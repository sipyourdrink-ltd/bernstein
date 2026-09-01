# Trust records (trace export)

`bernstein trace export` emits a **TRACE 0.2 Trust Record** — a signed
JWT-style JSON blob (Ed25519) that proves a run's journal chain is intact
and binds the run to the install identity. It is generated entirely
offline from local state; no OTLP endpoint or network is required.

```
bernstein trace export <RUN_ID> [--out PATH] [--json] [--last] [--sdd-dir PATH]
```

- `--last` picks the most recently finished run in `.sdd/runs/`
  (a directory with a non-empty `journal.jsonl`), sorted by mtime.
- `--out` writes the canonical JSON string to a file instead of stdout.
- `--json` emits the canonical JSON form (identical to the default output).
- `--sdd-dir` overrides the `.sdd/` path; defaults to `./.sdd/` or `./.`.

Exit codes: `0` = exported, `1` = run not found / chain broken / emit
error, `2` = missing `RUN_ID` argument.

The trace extra (`bernstein[trace]`) is required.

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

Then verify a record file against the TRACE v0.2 schema at Level 1
(checks claims beyond the bare schema, including cross-field invariants):

```bash
trace-tests verify --record <file> --level 1
```

Level 0 validates the schema and signature only; Level 1 adds
cross-field invariants such as SPIFFE subject scoping and delegation
parent-child hash binding. Use Level 1 for production records.

### Vectors for testing

Four committed test vectors under
`tests/fixtures/trust-record-vectors/` cover the four record shapes:

- `single-execution-trust-record.json` — root, non-delegated execution
- `delegated-parent-trust-record.json` — parent hop of a two-hop run
- `delegated-child-trust-record.json` — child hop, carries `delegation`
- `aggregate-trust-record.json` — run-level rollup, carries
  `references[rel=member-execution]`

Run the reference suite against any one:

```bash
uv run --with agentrust-trace-tests==0.5.1 trace-tests verify \
    --record tests/fixtures/trust-record-vectors/single-execution-trust-record.json \
    --level 1 --max-age 999999999999
```

(`--max-age` is set far above the default 24 h window because the
fixture vectors use a frozen 2023-11-14 clock, not wall-clock time —
an unmodified default would reject all four as stale.)

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
