---
search:
  boost: 2
---

# Datasources & query receipts

When an agent reasons over operational data, a query result usually enters the
prompt as plain text: the model cites numbers from it, the run completes, and
nothing records **which exact rows the model saw**. Lineage covers file writes;
attachments are content-addressed anchors; MCP results have an artefact kind —
but a pasted query result is unrecorded.

A **query receipt** closes that gap. A read-only datasource connection runs a
`SELECT`, the result set is canonicalised into a stable byte form, and its
SHA-256 is bound into a signed lineage entry with a `.jws` sidecar. The result
handed to the agent carries the receipt id; the receipt **is** the "these are
the bytes the model saw" record — content-addressed and offline-verifiable.

---

## TL;DR

| Step | Command |
|------|---------|
| Register a read-only connection | `bernstein datasource register <id> <dsn>` |
| Run a query, emit a receipt | `bernstein datasource query <id> "<SELECT ...>"` |
| Verify a receipt offline | `bernstein datasource verify <receipt-id>` |
| Detect drift against live data | `bernstein datasource verify <receipt-id> --re-execute` |

- Connections are **read-only**. Any DML/DDL is refused with a typed error.
- A receipt records only the connection **id** — never the DSN, so secrets stay
  out of receipts and logs.
- Re-running the same query later tells you `MATCH` or `DRIFT`, turning "the
  data changed under the agent" from a suspicion into a named, provable event.

---

## Connections

A connection is an operator-configured, read-only SQL source addressed by a
stable id.

```bash
bernstein datasource register sales /var/data/sales.db
bernstein datasource register sales ":memory:" --description "scratch"
bernstein datasource list
```

- **Driver.** The baseline driver is `sqlite` (the reference engine). The DSN is
  a database file path or `:memory:`.
- **Secrets.** A warehouse DSN may carry a password (`scheme://user:pass@host/db`).
  It is stored only in the operator-only registry (`.sdd/datasources/connections.json`,
  mode `0600`) and is **redacted** (`user:***@`) anywhere it is displayed. A
  receipt never contains a DSN.

!!! note "Engine support"
    The reference engine is stdlib `sqlite3`, opened read-only (`mode=ro`) with a
    SQL authorizer that denies every write action. The canonical encoding is
    **engine-agnostic**: any adapter that normalises its result onto the shared
    schema/rows shape produces the same `content_hash`. An Arrow-interchange
    warehouse adapter (DuckDB, Postgres, …) is a planned follow-up and slots in
    behind the same interface without changing the receipt format.

---

## Running a query

```bash
bernstein datasource query sales "SELECT region, SUM(amount) AS total \
  FROM orders GROUP BY region ORDER BY region"
```

Output is the text an agent would read, followed by the receipt id:

```
region (text) | total (float)
apac | 22.0
emea | 17.75
(2 rows)

Receipt sha256:54d7...78a4  content_hash=sha256:00ea...  rows=2
```

Add `--json` to get the full receipt object, `--param VALUE` (repeatable) for
positional bind parameters, and `--row-cap N` to cap the result.

### Read-only enforcement

Only a single read statement is accepted. These are all refused with a typed
error, before execution:

```bash
bernstein datasource query sales "DELETE FROM orders"          # ReadOnlyViolation
bernstein datasource query sales "SELECT 1; DROP TABLE orders" # UnsupportedStatement (multi)
bernstein datasource query sales "WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x"  # ReadOnlyViolation
```

---

## The canonical encoding

`content_hash = sha256(canonical_bytes(result))`. The encoding is fixed and
versioned so a digest can never silently change meaning:

| Property | Rule |
|----------|------|
| Schema line | column name + canonical type, in select order |
| Rows | in result order; row and column order are significant |
| Framing | every field length-prefixed; each cell carries a one-byte type tag |
| Integer | exact base-10 |
| Float | shortest round-trip (`repr`) |
| Decimal | fixed-point, scale preserved (`1.50` ≠ `1.5`) |
| Boolean | `1` / `0` |
| Text | UTF-8, **must be NFC** (non-NFC text is rejected, never normalised) |
| Blob | raw bytes (binary-safe) |
| NULL | a distinct sentinel — never collides with the empty string or the text `"NULL"` |
| Truncation | the truncation flag **and** the row cap are inside the hashed body |

Because the truncation flag is hashed, a truncated result can never re-hash to
the same digest as the full result it was cut from.

!!! note "Decimals on SQLite"
    SQLite has no decimal type: a `DECIMAL`/`NUMERIC` column carries NUMERIC
    affinity that converts `"1.50"` to the real `1.5` on insert, so the fixed
    scale is gone before the engine ever reads it. The reference engine
    therefore surfaces such columns as homogeneous `float`. Faithful fixed-scale
    decimals are a property of the canonical encoding itself and are surfaced by
    any decimal-carrying engine (the future Arrow-interchange adapter).

---

## Verifying a receipt

```bash
bernstein datasource verify <receipt-id>
```

Offline verification checks each field and names the first that fails:

| Check | What it proves |
|-------|----------------|
| `lineage_entry` | the signed anchor entry exists in the log |
| `signature` | its detached Ed25519 JWS verifies against the agent card |
| `operator_hmac` | its HMAC envelope recomputes under the operator key |
| `content_hash` | the signed entry's digest matches the receipt's result hash |
| `receipt_body` | every receipt-core field matches the digest bound into the entry |
| `result_copy` | any stored result copy re-hashes to `content_hash` |

Tampering the stored result copy, the receipt body, or the chain anchor makes
`verify` fail at the named field. Removing the lineage entry makes the receipt
**unverifiable** — the receipt's integrity comes entirely from the signed entry
it projects, so a detached receipt attests nothing.

### Drift detection

```bash
bernstein datasource verify <receipt-id> --re-execute
```

Re-runs the receipt's recorded query (with its parameters and row cap) against
the connection and reports:

- `MATCH` — the live result re-hashes to the recorded `content_hash`.
- `DRIFT` — the hashes differ; both are printed alongside the row counts.

The connection is looked up by the operator (or `--connection <id>`), never
taken from the receipt, so re-execution never trusts a DSN from untrusted data.

!!! warning "Ground on deterministic queries"
    Drift is only meaningful for a query whose result is a pure function of the
    data. A query that calls a non-deterministic function — `random()`,
    `randomblob()`, `datetime('now')` and friends — hashes over volatile bytes,
    so a re-execution reports `DRIFT` on every run even when nothing in the data
    changed. Ground agents on deterministic `SELECT`s; treat drift on a
    volatile-function query as expected noise, not a data-change signal.

---

## On-disk layout

Everything lives under `.sdd/datasources/`:

```
connections.json          operator-only connection registry (may hold secrets)
identity/                 Ed25519 signing identity + published agent card
lineage/                  append-only log.jsonl + .jws signature sidecars
receipts/<hash>.json      one receipt per execution
results/<hash>.bin        optional, size-capped, re-hashable result copies
receipts-audit.jsonl      additive, secret-free audit mirror
```

The audit mirror records the connection id, the query/params/content hashes, the
row count and the truncation flag — never a DSN and never raw query parameters.
