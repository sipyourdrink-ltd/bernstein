# Tracker audit log

Every state move made by a Bernstein agent against a ticket tracker
(claim, comment, transition, attach, fail) is recorded as a signed
content-addressed JSONL entry. The artefact is auditor-readable and
maps directly to the per-decision evidence requirements asked for by
SOX, SOC 2 Type II, and the EU AI Act.

## On-disk layout

* Path: `.sdd/lineage/tracker_audit.jsonl`
* Format: append-only JSONL, one entry per line, RFC 8785 JCS canonical
  bytes.
* The file is gitignored; operator-side WORM storage is out of scope of
  this module (see the *Out of scope* section below).

## Entry schema (`schema_version: 1`)

| Field | Type | Description |
|-------|------|-------------|
| `schema_version` | int | Always `1` in this release. Bumping the value requires a parallel reader for the prior version. |
| `id` | string | UUIDv7 hex; time-sortable when running on Python 3.13+. |
| `ts_ns` | int | Nanoseconds since the Unix epoch. |
| `prev_entry_hash` | string | `sha256:` digest of the previous entry, or `sha256:000...0` for genesis. |
| `entry_hash` | string | `sha256:` digest of the entry's canonical bytes with `entry_hash` and `signature` blanked. |
| `tracker_name` | string | Adapter name (`jira`, `linear`, `github`, ...). |
| `ticket_id` | string | Stable tracker-side identifier. |
| `etag_before` | string\|null | Optimistic-concurrency etag observed before the call. |
| `etag_after` | string\|null | Etag returned by the tracker after the call. |
| `action` | string | One of `claim`, `comment`, `transition`, `attach`, `fail`. |
| `actor.session_id` | string | Bernstein session id. |
| `actor.role` | string | Role prompt that produced the call (`backend`, `qa`, ...). |
| `actor.model` | string | Model identifier used for the call. |
| `input_prompt_hash` | string | `sha256:` digest of the input prompt bytes. |
| `output_blob_hash` | string | `sha256:` digest of the response payload bytes. |
| `cost_usd` | float | USD spend attributed to the call. |
| `tokens_in` | int | Input tokens. |
| `tokens_out` | int | Output tokens. |
| `idempotency_key` | string\|null | Tracker-side idempotency key, when supplied. |
| `lifecycle_event_id` | string\|null | Cross-link to the originating lifecycle hook event. |
| `signature` | string | HMAC-SHA256 over the canonical bytes (with `signature` blanked) under the operator secret. |
| `failure_category` | string\|null | Exception class name when `action == "fail"`. |
| `failure_detail` | string\|null | Truncated failure message (max 512 chars). |

The auditor verifies the chain end-to-end by walking each line, hashing
the canonical bytes, and comparing both the `entry_hash` and the HMAC
signature.

## Field-to-question mapping

| Auditor question | Field(s) |
|------------------|----------|
| SOX-2026: which automated control changed state at `<time>`? | `ts_ns`, `tracker_name`, `ticket_id`, `action` |
| SOC 2 Type II CC7.1 (change identification): who initiated the action? | `actor.session_id`, `actor.role`, `actor.model` |
| EU AI Act Article 12 logging: which model and prompt produced the output? | `actor.model`, `input_prompt_hash`, `output_blob_hash` |
| SOC 2 CC6.1 (logical access): is the chain tamper-evident? | `prev_entry_hash`, `entry_hash`, `signature` |
| Cost-attribution: how much did the change cost? | `cost_usd`, `tokens_in`, `tokens_out` |
| Concurrency control: what etags did we observe? | `etag_before`, `etag_after` |
| Replay: how do I match this entry to the orchestrator's lifecycle event? | `lifecycle_event_id` |
| Idempotency: did a retry collide with an earlier call? | `idempotency_key` |

## Operator commands

```sh
# Show the chronological view, optionally filtered by tracker / ticket.
bernstein lineage tracker-audit show --tracker jira --ticket PROJ-1

# Periodic export window (nanoseconds since the Unix epoch).
bernstein lineage tracker-audit show --since 1715000000000000000

# Auditor bundle.
bernstein lineage tracker-audit export --output /tmp/bundle.jsonl

# Tamper detection. Exits non-zero on the first offending line.
bernstein lineage tracker-audit verify
```

All commands read the HMAC operator secret from the
`BERNSTEIN_OPERATOR_SECRET` environment variable by default; override
with `--secret-env`.

## Wiring an adapter

Adapters opt in by being wrapped at the orchestrator boundary. The
shipped wrapper:

```python
from bernstein.core.lineage import LineageCtx, TrackerActor, TrackerAuditLog, wrap_adapter

log = TrackerAuditLog(Path(".sdd/lineage/tracker_audit.jsonl"), hmac_key=key)
ctx = LineageCtx(log=log, actor=TrackerActor(session_id=sid, role=role, model=model))
adapter = wrap_adapter(jira_adapter, ctx)
adapter.add_comment("PROJ-1", "hello", idempotency_key="idem-1")
```

The wrapper emits one entry on success and one entry with
`action="fail"` on exception, then re-raises so the orchestrator's
retry policy keeps working.

## Out of scope

* WORM storage of the JSONL. Pipe the file to your evidence store of
  choice (S3 Object Lock, Azure Immutable Blob, GCS Bucket Lock).
* Auto-ingestion into vendor SIEMs. File a separate ticket per SIEM.
* Sigstore attestation on top of the HMAC envelope. Tracked as a
  follow-up.
