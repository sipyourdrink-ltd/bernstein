# Regulatory lineage export - operator guide

A practical walk-through for a compliance officer or platform operator
who needs to take a Bernstein run and hand it to an auditor. Covers the
`bernstein lineage export` and `bernstein lineage verify` commands,
what the export contains, how to verify the chain, retention rules,
and worked sample workflows for the regulators most often named in the
wild (EU DORA / NIS2, SOC 2, EU AI Act, HIPAA).

For schema details, the regulator-class vocabulary, and the customer-
key signing model, see
[Regulator-class lineage](regulatory-lineage.md). This page is the
"how to ship the artefact" companion to that one.

---

## What the lineage trail captures

Every file write performed by an agent emits one `LineageRecord`
linking a single output region back to the prompt, model, and inputs
that produced it. Records land in the existing HMAC-chained WAL under
`.sdd/runtime/wal/<run_id>.wal.jsonl` with `decision_type=lineage`.
One record (schema v2):

```json
{
  "schema_version": 2,
  "output_artifact": {
    "path": "rules/srv-001.yml", "sha256": "9f86d0…",
    "line_start": 1, "line_end": 42
  },
  "inputs": [{"path": "playbooks/baseline.yml", "sha256": "1c61e3…"}],
  "producer": {
    "agent_id": "claude-sonnet-3",
    "run_id": "r-2026-05-05", "tick_id": "t-114"
  },
  "prompt_sha": "6f51ad…", "model": "claude-sonnet",
  "cost_usd": 0.0042, "tokens": 312, "timestamp": 1714896000.0,
  "regulatory_class": "production_detection_rule",
  "customer_signature": "<base64-detached-Ed25519-sig>"
}
```

A regulator typically asks about: `output_artifact` ("which exact
bytes?"), `inputs` ("what did the agent read first?"), `producer`
("which agent run, which tick?"), `prompt_sha` / `model` ("which
prompt and model produced this?"), `cost_usd` / `tokens` (compute
attribution), `regulatory_class` (operator-supplied filter, below),
and `customer_signature` (detached Ed25519 signature a regulator can
verify without Bernstein).

Source: `src/bernstein/core/persistence/lineage.py:96`.

---

## The `regulatory_class` field

Free-text label, operator-supplied. Bernstein does not enforce a
vocabulary - the same record might be DORA evidence in one tenant and
SOC 2 CC7 evidence in another. Three places set the class, in priority
order:

1. **Per-step in code** - the writer that emits the record passes an
   explicit `regulatory_class=` value.
2. **Per-task in `bernstein.yaml`** - set
   `tuning.lineage.regulatory_class_default` for the whole run; the
   `LineageWriter` falls back to this when a record has no explicit
   class
   (`src/bernstein/core/persistence/lineage.py:374`).
3. **Unset (`null`)** - the record is still written; the export shows
   `-` (HTML) or an empty cell (CSV).

There is no gate that blocks writes when the class is missing. If your
auditor needs every record classified, set the run-level default and
treat any `null` in the export as an exception worth investigating.
Recommended labels per regulator are listed in
[Regulator-class lineage § Recommended vocabulary](regulatory-lineage.md#recommended-regulatory_class-vocabulary).

---

## Customer-key Ed25519 signing (summary)

The customer signature is the half of the audit story a regulator can
verify without trusting Bernstein. The HMAC chain proves "not edited
inside Bernstein"; the customer signature proves "came out of the run
with the customer's own key in the loop." Minimal setup:

```bash
openssl genpkey -algorithm Ed25519 -out customer-ed25519.pem
openssl pkey -in customer-ed25519.pem -pubout \
        -out customer-ed25519-pub.pem
chmod 600 customer-ed25519.pem
```

```yaml
# bernstein.yaml
tuning:
  lineage:
    customer_signing_enabled: true
    customer_signing_key_path: /etc/bernstein/customer-ed25519.pem
    customer_signing_key_kind: ed25519
    regulatory_class_default: "production_detection_rule"
```

Full configuration (key kinds, HSM / TPM / KMS integration via the
`LineageSigner` protocol, key-rotation guidance) is in
[Regulator-class lineage § Customer-key signature](regulatory-lineage.md#customer-key-signature).

---

## Producing the export

```bash
bernstein lineage export <run_id> --format <csv|jsonld|html> \
                         --output <path> [--workdir .]
```

Source: `src/bernstein/cli/commands/lineage_export_cmd.py`.

The exporter walks every lineage record for the run from
`.sdd/runtime/wal/`, flattens each record into a row, and renders the
chosen format. The output file is overwritten if it exists.

| Format   | When to use                                                                            |
| -------- | -------------------------------------------------------------------------------------- |
| `csv`    | Analyst spreadsheets, GRC vendors that ingest CSV, ad-hoc filtering in Excel / Sheets. |
| `jsonld` | Evidence packs for graph-walking auditors; shaped against schema.org `Action`.         |
| `html`   | Human auditor review; single self-contained file (no JS / fonts / external assets).    |

Exit codes:

| Code | Meaning                                             |
| ---- | --------------------------------------------------- |
| `0`  | Export succeeded.                                   |
| `1`  | `.sdd/` directory not found at the given workdir.   |
| `2`  | No lineage records exist for the supplied `run_id`. |

The `2` exit is useful in CI: a run that should have produced records
but did not is treated as a fast failure, not an empty file.

```bash
# Human-readable HTML for an auditor packet.
bernstein lineage export r-2026-05-05 --format html \
  --output /tmp/r-2026-05-05.html

# CSV for a GRC vendor.
bernstein lineage export r-2026-05-05 --format csv \
  --output /tmp/r-2026-05-05.csv

# JSON-LD for a graph-walking verifier.
bernstein lineage export r-2026-05-05 --format jsonld \
  --output /tmp/r-2026-05-05.jsonld
```

The HTML form is suitable for direct inclusion in an evidence package
(open in any browser, print to PDF, attach to the audit ticket).

---

## Verifying the chain

### `bernstein lineage verify` - operator path

```bash
bernstein lineage verify <run_id> [--workdir .] \
                         [--public-key /path/to/customer-pub.pem]
```

Source: `src/bernstein/cli/commands/lineage_verify_cmd.py`. Walks
every record for the run, re-checks the WAL hash chain, and (when
`--public-key` is supplied) re-verifies every `customer_signature`.

Exit codes:

| Code | Meaning                                                                |
| ---- | ---------------------------------------------------------------------- |
| `0`  | Chain intact and (if `--public-key`) every signature validated.        |
| `1`  | `.sdd/` not found at the given workdir, or supplied public key is bad. |
| `2`  | Tamper detected: WAL chain broken, or one or more signatures invalid.  |

The command prints a per-error list capped at 50 entries; the rest are
summarised. Pipe to a file for the full set on a noisy chain.

### Auditor path - verify without Bernstein in the loop

A customer auditor with only the public key and the WAL files can
re-verify every signature without any Bernstein machinery in the
critical path:

```python
from bernstein.core.persistence.lineage import (
    LineageReader, canonical_record_bytes, decode_signature,
)
from bernstein.core.persistence.lineage_signer import (
    Ed25519PublicKeyVerifier,
)

verifier = Ed25519PublicKeyVerifier.from_path("customer-ed25519-pub.pem")
reader = LineageReader(sdd_dir=".sdd")
for rec in reader.iter_records(run_id="r-2026-05-05"):
    if rec.customer_signature is None:
        continue  # v1 record or signing disabled
    sig = decode_signature(rec.customer_signature)
    assert verifier.verify(canonical_record_bytes(rec), sig)
```

### Tamper-loud detection in the janitor

The janitor's compaction step runs the same `verify_run_chain()` on
every cycle. On verification failure it (a) emits an `audit.jsonl`
entry of type `lineage_tamper_detected`, (b) increments the
`bernstein_lineage_tamper_total{run_id=...}` Prometheus counter, and
(c) POSTs to the configured SIEM webhook with exponential back-off on
5xx. The janitor never blocks on a bad webhook - it records the event
and lets the operator decide response policy. Source:
`src/bernstein/core/quality/janitor.py:187`. Webhook configuration:
[Regulator-class lineage § SIEM webhook](regulatory-lineage.md#configuring-the-siem-webhook).

---

## Retention rules

Lineage records share storage with the run's WAL and inherit the
janitor's retention policy:

| Knob                         | Default | Source                                                       |
| ---------------------------- | ------: | ------------------------------------------------------------ |
| `disk.run_retention_count`   |     20  | `core/defaults.py:419`. Last 20 runs kept; older are pruned. |
| `disk.wal_retention_count`   |     50  | `core/defaults.py:421`. Last 50 WAL files per run kept.      |
| `lineage.compaction.enabled` |  `true` | Janitor gzips rotated WAL files in place.                    |

Two operational rules:

1. The active `<run_id>.wal.jsonl` is never compressed by the janitor
   while writes are still happening - a live verifier never races the
   compactor.
2. Rotated `<run_id>.wal.jsonl.<N>` segments are gzipped to `<...>.gz`
   on the next janitor cycle. The reader handles both forms
   transparently.

For an auditor handover, snapshot `.sdd/runtime/wal/` for the run
*before* the next janitor cycle and ship the snapshot alongside the
export. The export itself is self-contained for review; the raw WAL is
what an auditor uses to re-verify the chain end-to-end.

If your retention policy requires longer-than-default storage, raise
both counts and point the WAL directory at a volume that survives node
replacement; lineage records have no separate retention surface.

---

## Worked workflows by regulator

### EU DORA (and NIS2)

Persona: a financial-services compliance lead preparing a DORA
Article 17 evidence pack for a SIEM-rule production change.

```yaml
# bernstein.yaml
tuning:
  lineage:
    regulatory_class_default: "production_detection_rule"
    customer_signing_enabled: true
    customer_signing_key_path: /etc/bernstein/dora-ed25519.pem
```

```bash
bernstein lineage verify r-2026-05-05 \
  --public-key /etc/bernstein/dora-ed25519-pub.pem

bernstein lineage export r-2026-05-05 --format html \
  --output /pack/r-2026-05-05.html
bernstein lineage export r-2026-05-05 --format jsonld \
  --output /pack/r-2026-05-05.jsonld
```

Bundle the HTML, the JSON-LD, the run summary, the audit-log export,
the public key, and the WAL snapshot. Hand to the auditor. The HTML
form plus the public key are sufficient for offline re-verification.

### SOC 2

Persona: an internal auditor preparing CC7.1 (system monitoring) and
CC7.2 (anomaly detection) evidence. Typical operator labels are
`"soc2_cc7_change"` for production-config edits and
`"soc2_cc8_release"` for release-management edits.

```bash
bernstein lineage verify r-2026-05-05

bernstein lineage export r-2026-05-05 --format csv \
  --output /grc-uploads/r-2026-05-05.csv
bernstein lineage export r-2026-05-05 --format html \
  --output /pack/r-2026-05-05.html
```

Pair the export with `security/AUDIT.md` (HMAC-chained audit log) for
change-management integrity. SOC 2 evidence is generally satisfied by
the audit log alone; lineage adds per-artefact provenance when an
auditor asks "which prompt produced this rule?"

### EU AI Act (Article 12)

Article 12 requires "automatic recording of events" sufficient for
post-market traceability. Lineage maps cleanly: each record is an
auto-generated event tying an output to its producing prompt, model,
and inputs. The JSON-LD form is the recommended shape for an Annex IV
appendix because a JSON-LD verifier can graph-walk the chain without a
custom parser.

```bash
bernstein lineage export r-2026-05-05 --format jsonld \
  --output /pack/article-12-r-2026-05-05.jsonld
```

Combine with the `bernstein compliance assess` evidence package
([compliance.md § assess](../operations/compliance.md#assess-generate-the-eu-ai-act-evidence-package))
for the full Article 43 handoff.

### HIPAA

HIPAA does not specify a "lineage" requirement directly; the relevant
hooks are 45 CFR §164.312(b) (audit controls) and §164.308(a)(1)(ii)(D)
(information system activity review). Lineage records satisfy both by
making "which agent, which prompt, which input bytes touched this PHI
artefact?" answerable in one command.

```yaml
# bernstein.yaml
compliance: hipaa
tuning:
  lineage:
    regulatory_class_default: "hipaa_audit_event"
    customer_signing_enabled: true
    customer_signing_key_path: /etc/bernstein/hipaa-ed25519.pem
```

```bash
bernstein lineage export r-2026-05-05 --format html \
  --output /pack/hipaa-r-2026-05-05.html
```

Pair the lineage export with the BAA report from
`core/security/hipaa.py` for the §164.314(a) business-associate
evidence.

> Lineage records inherit whatever PII / PHI redaction the audit log
> applies upstream; there is no extra redaction layer at lineage write
> time. Confirm `core/security/pii_gating.py` is configured for your
> PHI categories before treating the export as PHI-safe.

---

## Limitations

- One signing key per run; rotation across environments is on the
  operator. The schema accommodates a key id prefix in the signature
  blob; no registry is shipped.
- The signature covers full canonical record bytes. Edits to any
  field - including downstream-derived fields - invalidate the
  signature. This is intentional; operators who need finer-grained
  signing can plug in a custom canonicaliser.
- `regulatory_class` is unconstrained free text. Recommended labels
  are documented; they are not enforced.
- No backfill: writes from before v1.10 have no lineage records.
- Single-run scope: cross-run chain stitching is out of scope today.
- Direct integration with specific GRC vendor APIs (ServiceNow GRC,
  Archer, etc.) is out of scope - the exporter formats are generic.

---

## Related

- [Regulator-class lineage](regulatory-lineage.md) - schema reference,
  customer-signing configuration, tamper-loud SIEM webhook setup.
- [Artifact lineage trail](../concepts/artifact-lineage.md) - base
  schema and chain-walking concepts.
- [Compliance](../operations/compliance.md) - `bernstein compliance`
  CLI for EU AI Act, SOC 2, ISO 27001, PCI-DSS, NIST 800-53, HIPAA.
- `security/AUDIT.md` - HMAC-chained audit log.
- Source: `src/bernstein/core/persistence/lineage.py`,
  `lineage_signer.py`, `core/observability/lineage_alert.py`,
  `cli/commands/{lineage_cmd,lineage_export_cmd,lineage_verify_cmd}.py`.
