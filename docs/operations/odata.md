# Generic OData v4 system-of-record integration

Operations teams run on business systems of record whose integration surface is
[OData v4](https://www.odata.org/) — order, purchase, service, and inventory
objects exposed as entity sets with stable `$metadata` introspection. Bernstein
integrates with them in two composable halves that share one deterministic
cursor and anchor every write to the audit chain:

- a **poll trigger source** (`odata_poll`) that turns "a record changed in the
  system of record" into a normal `TriggerEvent`, and
- a **receipt-anchored write-back helper** (`odata_writeback`) that writes an
  outcome back under optimistic concurrency and records a signed receipt.

Neither half assumes anything vendor-specific: the change-timestamp property,
the key properties, and the auth scheme are all per-connection config, because
no single convention holds across suites.

## Trigger source: `odata_poll`

### Watermark polling is the baseline

Each poll issues `GET /<EntitySet>?$filter=<ts_prop> gt <watermark>` ordered by
the timestamp, emits one `TriggerEvent` per changed entity, and advances the
watermark to the largest timestamp it saw. Because the filter is strictly
`gt`, an entity at exactly the watermark is excluded on the next poll, so a
restart never re-emits it. There is no assumed timestamp property name — set
`timestamp_property` per connection.

Server-driven paging is followed transparently: the source walks every
`@odata.nextLink` page before returning.

### Delta links are opt-in, never assumed

With `prefer_delta` set, the first poll probes with the
`Prefer: odata.track-changes` request header. Only if the service actually
returns an `@odata.deltaLink` does the source switch to delta-token paging;
subsequent polls follow the stored delta link and surface deletions as
`delete` events. If the service ignores the header (some view-backed entity
sets silently omit delta links), the source stays on watermark polling.

Any delta read failure downgrades to watermark polling **without losing the
cursor** — the watermark is maintained in both modes, so the resume position
survives the downgrade.

### The cursor is a deterministic, resumable record

| Cursor field | Meaning |
|---|---|
| `entity_set` | the set the cursor belongs to |
| `mode` | `watermark` or `delta` |
| `watermark` | last-seen maximum change timestamp (always maintained) |
| `delta_link` | the `@odata.deltaLink` to follow next (delta mode) |
| `last_page_hash` | `sha256:` of the last result page |
| `probed` | whether a delta probe was already attempted |

Persist it with `save_cursor(path, cursor)` and reload with
`load_cursor(path)`. The serialized form is canonical bytes, so two operators
replaying the same timeline from the same persisted cursor derive byte-identical
events — no duplicate, no drop. `last_page_hash` lets an auditor prove which
window of changes produced which tasks.

### Rate handling

- `429` responses are honored: the client sleeps the `Retry-After` value (capped
  by `max_retry_after_s`) and retries, up to a bounded budget.
- `rate_limit_min_interval_s` enforces a minimum gap between HTTP calls for
  vendors that document a fixed per-API minimum.

Both use an injected clock, so tests exercise them with a fake clock and no real
sleeps.

### Auth

`OdataAuth` supports `bearer`, `api_key`, and `oauth2_client_credentials`, plus
`none`. Credentials are read from environment variables at call time and are
never written to a log — auth header values are redacted before any debug line
is emitted.

## Write-back: `odata_writeback`

`update_entity(connection, key, patch, chain=...)` implements GET-before-PATCH:
it reads the current entity and its ETag, then sends `If-Match`. It refuses to
write blindly:

| Situation | Result |
|---|---|
| No fresh ETag exposed by the service | `OdataConflict(status=428)` |
| Stale ETag (concurrent edit landed) | `OdataConflict(status=412)` |
| Match | PATCH applied, receipt recorded |

Draft-enabled objects are supported through `draft_flow` and
`draft_activate_action`: the helper creates a draft, patches it under its ETag,
then POSTs the bound activate action.

### Every write-back emits a receipt

A successful write-back appends an `odata.writeback_receipt` event to the HMAC
audit chain, binding:

| Field | Binds |
|---|---|
| `connection` | the connection label |
| `entity_set` / `entity_key` | the record written |
| `etag_observed` | the concurrency token the PATCH was gated on |
| `payload_content_hash` | `sha256:` of the canonical sent payload |
| `http_status` | the status the write returned |
| `draft_flow` / `activate_action` | draft promotion, when used |

The entity body and any credential are never stored — only identifiers and
hashes. The returned `WriteBackReceipt` carries the same fields plus the HMAC of
the anchored event, so the receipt is bound to a specific chain position.

Because the receipt is a chain event, `bernstein audit verify` covers it with no
new verb: a mutated row breaks the chain at its exact position, and an auditor
holding the sent body re-hashes it and matches `payload_content_hash`.

## Worked example

Poll an `Orders` entity set, seed a task on each change, write the outcome back,
and verify the receipt offline.

```python
from pathlib import Path

from bernstein.core.security.audit_chain import AuditChainStore
from bernstein.core.trigger_sources.odata_poll import (
    OdataAuth,
    OdataConnection,
    OdataPollSource,
    load_cursor,
    save_cursor,
)
from bernstein.core.trackers.odata_writeback import update_entity

connection = OdataConnection(
    service_root="https://erp.example.com/odata/v4/svc",
    entity_set="Orders",
    timestamp_property="LastChangeDateTime",
    key_properties=("OrderID",),
    prefer_delta=True,              # probe for delta; fall back to watermark
    rate_limit_min_interval_s=1.0,  # respect a documented per-API minimum
    auth=OdataAuth(kind="bearer", token_env="ERP_TOKEN"),
    name="erp_orders",
)

cursor_path = Path(".sdd/odata/erp_orders.cursor.json")
source = OdataPollSource(connection)

# 1. Poll -> one TriggerEvent per changed order.
result = source.poll(load_cursor(cursor_path))
for event in result.events:
    seed_task_from(event)         # your normal trigger -> task path

# 2. Persist the advanced cursor so a restart resumes byte-identically.
save_cursor(cursor_path, result.cursor)

# 3. Write an outcome back under optimistic concurrency, recording a receipt.
chain = AuditChainStore(Path(".sdd/audit"))
receipt = update_entity(
    connection,
    {"OrderID": 4711},
    {"ProcessingStatus": "Reviewed"},
    chain=chain,
)
print(receipt.payload_content_hash, receipt.http_status)
```

Verify the write-back offline — no new verb:

```bash
bernstein audit verify --hmac-only
```

The command exits non-zero if any `odata.writeback_receipt` row was altered
after the fact.

## Notes

- No new external dependencies: the integration is built on the HTTP client and
  audit chain the repo already ships.
- The full protocol surface (server-driven paging, delta links behind the
  `Prefer` header, ETags, `412`/`428`, `429` + `Retry-After`, draft-activate) is
  covered by an in-process fake OData service in `tests/unit/odata/`, driven
  with an injected clock and no network.
