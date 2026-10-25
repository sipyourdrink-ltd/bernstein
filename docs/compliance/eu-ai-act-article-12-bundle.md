# EU AI Act Article 12 evidence pack

A practical guide for the operator who has to assemble Article 12-conformant
record-keeping evidence for a conformity assessment. Covers what the bundle
contains, how to build it, how the retention pin works, and how an auditor
verifies it offline.

For the underlying chain integrity story, see
[audit log](../security/audit-log.md). For the third-party-verifiable
wrapper around the bundle, see
[DSSE / in-toto envelope](../security/audit-dsse-envelope.md).

## What Article 12 wants

Regulation (EU) 2024/1689 Article 12 ("Record-keeping") and Article 19(1)
("Automatically generated logs") together require that high-risk AI systems
keep automatic event-logs over the lifetime of the system, retain them at
least 6 months (10 years for high-risk classifications), and produce them
on request for a conformity assessment.

The Article 12 bundle is the smallest viable artefact that satisfies
those requirements. Source:
`src/bernstein/core/security/article12_bundle.py`.

## What the bundle contains

The bundle is a single deterministic zip with four entries. Same input
window + same risk class → byte-identical archive.

| Entry | Purpose | Article 12 anchor |
|---|---|---|
| `manifest.json` | Schema version, bundle id, window bounds, risk class, event count, chain anchor (head HMAC), per-artefact SHA-256, retention block. | 12(2)(c) - chain anchor; 12(3) - retention pin. |
| `events.jsonl` | The HMAC-chained audit slice for the window, sorted, byte-identical to the on-disk source records. | 12(1) - automatic recording. |
| `data_catalog.json` | Per-resource activity counts (input/output catalog). | 12(2)(b) - post-market monitoring; Annex IV §2(d) - data governance. |
| `clause_map.json` | Maps each artefact above to its Article 12 sub-clause. | Auditor navigation. |

Determinism rules applied to the zip:

- Fixed file order (alphabetical).
- Fixed mtime (1980-01-01 - the floor zip can encode).
- Stored mode `0644`.
- Canonical JSON (sorted keys, two-space indent, trailing newline) for every
  `.json` entry; `events.jsonl` is one sorted-key JSON object per line.

Same input bundle bytes hash to the same SHA-256 every run. The auditor
re-runs the build a year later and compares the archive's SHA-256
(`sha256sum article12_<bundle_id>.zip`, or `bundle.sha256` from the Python
API) against the published value. The CLI summary shows only the first 16
hex characters.

## Retention pin

`manifest.json:retention` carries:

| Field | Meaning |
|---|---|
| `risk_class` | `high`, `limited`, or `minimal`. |
| `retention_days` | Computed retention horizon. 10 years (3652 days) for `high`; 183 days minimum otherwise. |
| `retention_until` | ISO-8601 date by which deletion is forbidden. |
| `last_event_ts` | ISO-8601 timestamp of the latest covered event (the anchor for the retention horizon). |

The pin is metadata, not an immutable storage backend. Pair the bundle
with S3 Object Lock, WORM Postgres, or `chattr +i` to make the retention
enforceable at the storage layer; the pin documents the obligation, the
backend enforces it.

`validate_retention()` re-checks the pin against the current clock during
verify so an auditor can assert "still within the Article 12(3) horizon"
without parsing dates by hand.

## Building a bundle

### CLI

```bash
bernstein audit export --article-12 \
    --since 2026-04-01T00:00:00+00:00 \
    --until 2026-05-01T00:00:00+00:00 \
    --risk-class high \
    --output .sdd/evidence/
```

Defaults: `risk-class=limited`, `output=<audit_dir>.parent/evidence/`. The
archive lands at `.sdd/evidence/article12_<bundle_id>.zip`.

### Python API

```python
from datetime import UTC, datetime
from pathlib import Path

from bernstein.core.security.article12_bundle import build_article12_bundle

bundle = build_article12_bundle(
    audit_dir=Path(".sdd/audit"),
    since="2026-04-01T00:00:00+00:00",
    until="2026-05-01T00:00:00+00:00",
    risk_class="high",
    output_dir=Path(".sdd/evidence/"),
)
print(bundle.bundle_id, bundle.event_count, bundle.archive_path, bundle.sha256)
```

Pass `write=False` to build the bundle in-memory only - useful for
spot-checking a window before shipping.

### Per-run anchor (alternative)

The default path slices the calendar-rotated `<sdd>/audit/*.jsonl` by
wall-clock window. For run-scoped evidence, `assemble_from_run` reads the
per-run audit slice at `<sdd>/runtime/audit/<run_id>.audit.jsonl` and
anchors the bundle to one orchestrator run rather than a wall-clock
window. Use this when the auditor's question is "show me everything that
happened during run X", not "show me everything in May".

## Verifying a bundle

### Narrow verifier (manifest hashes + retention)

```python
from pathlib import Path
from bernstein.core.security.article12_bundle import verify_bundle

result = verify_bundle(Path(".sdd/evidence/article12_<bundle_id>.zip"))
if not result.ok:
    for err in result.errors:
        print("FAIL:", err)
    raise SystemExit(1)
```

`verify_bundle` confirms two properties without needing the operator's
HMAC key:

1. Every artefact's SHA-256 in `manifest.json:artefacts` matches the
   on-disk bytes.
2. The retention pin still satisfies Article 12(3) at the current clock.

A bad zip, a mismatched hash, or an expired retention horizon flips
`result.ok=False` with a per-check error.

### HMAC chain verification (operator path)

To re-walk the embedded `events.jsonl` HMAC chain, the verifier needs the
operator's HMAC key:

```python
from bernstein.core.security.audit import AuditLog

# Extract events.jsonl from the bundle, then:
log = AuditLog(audit_dir=extracted_dir)  # hmac_key resolved per the audit-log doc
result = log.verify()
```

See [audit log](../security/audit-log.md) for key resolution semantics.

### Third-party-verifiable path

The narrow verifier above does not give an external auditor a
verifiable-without-the-HMAC-key signal. The DSSE / in-toto envelope wraps
the bundle in an Ed25519-signed payload that any party with the public
key can verify offline:

```bash
python tools/verify_audit_dsse.py \
  --envelope path/to/envelope.json \
  --bundle path/to/article12_<bundle_id>.zip \
  --public-key path/to/operator-pubkey.pem
```

The standalone verifier depends only on the Python standard library plus
`cryptography` and refuses to import anything from the `bernstein.*`
package. See
[DSSE / in-toto envelope](../security/audit-dsse-envelope.md) for the wire
format and the four-check verification model.

## Risk-class selection

The operator picks the risk class at build time. Bernstein does not
auto-assign; the EU AI Act risk classifier in
`core/security/eu_ai_act.py` runs at task time, and the operator decides
which class to pin into the evidence bundle based on the system's
deployment context.

| Class | Retention | When to use |
|---|---|---|
| `high` | 10 years (3652 days) | Annex III high-risk domains: biometrics, critical infrastructure, education, employment, essential services, law enforcement, migration, justice. |
| `limited` | 183 days minimum | Article 50 transparency-only systems. |
| `minimal` | 183 days minimum | Article 5-allowed minimal-risk systems. |

A pin lower than the system's actual risk class will fail at audit time;
a pin higher just keeps logs longer. When in doubt, pin `high`.

## Bundle index for multi-bundle deployments

Article 19(1) wants logs retained ≥6 months across the lifetime of the
system. For long-lived deployments that produce one bundle per assessment
window, the operator typically maintains a `bundle_index.json` alongside
the bundles:

```json
{
  "system_id": "bernstein-orch-prod-eu-1",
  "bundles": [
    {
      "bundle_id": "<sha256-prefix>",
      "since": "2026-04-01T00:00:00+00:00",
      "until": "2026-05-01T00:00:00+00:00",
      "risk_class": "high",
      "archive_sha256": "<hex>",
      "envelope_sha256": "<hex>"
    },
    ...
  ]
}
```

The index is operator-maintained - bernstein does not generate it. It is
the artefact the auditor asks for first ("show me every bundle for this
system over the last six months") and the only thing that demonstrates
continuity at the lifetime granularity.

## Compliance mapping

| Article 12 clause | Bundle artefact |
|---|---|
| 12(1) - automatic recording over lifetime | `events.jsonl`. |
| 12(2)(a) - Art. 79(1) risk situations + substantial modifications | Operator-side: surface the assessment via the `eu_ai_act.py:assess_task()` per-task store. The bundle does not auto-flag substantial modifications. |
| 12(2)(b) - post-market monitoring | `data_catalog.json` aggregates per-resource activity counts. |
| 12(2)(c) - third-party-verifiable monitoring of high-risk systems (Art. 26(5)) | `chain_anchor` + DSSE envelope (see [DSSE](../security/audit-dsse-envelope.md)). |
| 12(3) - retention horizon | `manifest.json:retention`; pair with an immutable backend for storage-side enforcement. |
| 19(1) - logs ≥6 months | Same as 12(3) plus operator-maintained bundle index. |

For the AIGF control map, see
[FINOS AIGF mapping](finos-aigf-mapping.md). For the regulator-class
lineage trail that pairs with the bundle, see
[regulatory lineage](regulatory-lineage.md).

## Limitations

- **Substantial-modification detection** is operator-side only. The bundle
  does not flag tasks that change the agent fleet's effective capabilities.
- **The retention pin is metadata, not enforcement.** Without an immutable
  backend, an attacker with `rm` can delete the bundle and the pin
  disappears with it.
- **Article 43 paperwork** (full conformity assessment) is not generated
  by the bundle. Use `bernstein compliance assess` for the Annex IV
  technical document and the Article 43 conformity-assessment record.
- **Bundle index** is not auto-generated; the operator maintains
  `bundle_index.json` themselves.
- **Wall-clock window vs run scope** are different code paths. The
  default builder reads the calendar-rotated daily logs;
  `assemble_from_run` reads the per-run slice. Pick one and document it
  in the operator runbook so the auditor sees consistent shape across
  bundles.

## Related

- [Audit log](../security/audit-log.md) - HMAC chain layout, key
  management, verify procedure.
- [DSSE / in-toto envelope](../security/audit-dsse-envelope.md) -
  third-party-verifiable wrapper around the bundle.
- [Multi-tenant audit-chain export](../security/audit-multitenant.md) -
  per-tenant slice of the same chain with optional RFC 3161 timestamping.
- [Regulatory lineage](regulatory-lineage.md) - per-artefact lineage
  trail with customer-key signatures.
- [Compliance CLI](../operations/compliance.md) - `bernstein compliance
  assess` for the Annex IV / Article 43 paperwork.
- Source: `src/bernstein/core/security/article12_bundle.py`,
  `src/bernstein/core/security/audit_dsse.py`,
  `tools/verify_audit_dsse.py`.
