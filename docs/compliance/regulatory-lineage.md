# Regulator-class lineage (schema v2)

This document describes the schema-v2 lineage record produced by
Bernstein from version 1.10 onward. It is targeted at customer
compliance teams operating under EU DORA, NIS2, or equivalent
sector-specific regimes.

## What v2 adds over v1

v1 lineage records carry the producer/prompt/cost information needed
to walk a chain. v2 adds two fields:

| Field | Purpose |
|---|---|
| `regulatory_class` | Free-text label (operator-supplied) so a compliance team can filter the chain by class (production rule, policy edit, etc.) without parsing file paths. |
| `customer_signature` | Detached Ed25519 signature over the canonicalised record bytes, produced by a customer-controlled signing key. Independent of Bernstein's HMAC chain. |

`schema_version` is now an explicit field. v1 records still in a WAL
are read back with both new fields as `null` and `schema_version=1`.

## Schema

```json
{
  "schema_version": 2,
  "output_artifact": {
    "path": "rules/srv-001.yml",
    "sha256": "9f86d…",
    "byte_start": null, "byte_end": null,
    "line_start": 1, "line_end": 42
  },
  "inputs": [
    { "path": "playbooks/baseline.yml", "sha256": "1c61…" }
  ],
  "producer": {
    "agent_id": "claude-sonnet-3",
    "run_id": "r-2026-05-05",
    "tick_id": "t-114"
  },
  "prompt_sha": "6f51…",
  "model": "claude-sonnet",
  "cost_usd": 0.0042,
  "tokens": 312,
  "timestamp": 1714896000.0,
  "regulatory_class": "production_detection_rule",
  "customer_signature": "<base64-detached-Ed25519-sig>"
}
```

## Recommended `regulatory_class` vocabulary

The class is operator-supplied; we do not enforce any vocabulary.
The following labels match the categories most often demanded by
EU DORA / NIS2 evidence packages:

| Class | When to use |
|---|---|
| `production_detection_rule` | A SIEM/SOAR rule that lands in production. |
| `policy_edit` | A change to access policy / IAM / firewall config. |
| `remediation_playbook` | An automated response runbook. |
| `automated_response` | A direct mitigation action. |
| `posture_query` | A read-only posture-management query. |
| `internal_research` | Exploratory artefact, never deployed. |

Operators should pin the default for a run via:

```yaml
# bernstein.yaml
tuning:
  lineage:
    regulatory_class_default: "production_detection_rule"
```

## Customer-key signature

The signature covers the canonical bytes returned by
`canonical_record_bytes(record)` - sorted-key UTF-8 JSON without
whitespace, with `customer_signature` excluded (a signer cannot sign
its own output). Verification is independent of Bernstein's HMAC
chain: a customer auditor with only the public key and the WAL files
can confirm that every record was signed by the customer's signing
key, with no Bernstein machinery in the loop.

### Configuring the file-key signer

```yaml
tuning:
  lineage:
    customer_signing_enabled: true
    customer_signing_key_path: /etc/bernstein/customer-ed25519.pem
    customer_signing_key_kind: ed25519
```

The default `Ed25519FileKeySigner` accepts either:

- a PEM-encoded PKCS#8 private key (recommended for human-managed
  keys), or
- a raw 32-byte seed (recommended for keys exported from a KMS/HSM).

### Plugging in an HSM, TPM, or KMS

The `LineageSigner` protocol is a single method:

```python
class LineageSigner(Protocol):
    def sign(self, payload: bytes) -> bytes: ...
```

Any HSM / TPM / KMS-backed signer can be implemented to satisfy this
protocol and injected into `LineageWriter(..., signer=...)`. The
file-key reference implementation ships in core; HSM / KMS adapters
are operator-provided.

#### `kms_adapter: hsm` requires a real integration

Setting `lineage.customer_signing.kms_adapter: hsm` in `bernstein.yaml`
does **not** ship a working PKCS#11 / Cloud-KMS client. The base
`HSMKMSAdapter` in `bernstein.core.security.lineage_kms` is a
documentation stub: every `sign()` call raises `NotImplementedError`.
To use the `hsm` selector in production, ship a subclass that
overrides both `sign()` and `public_key_jwk()`, import it before the
orchestrator loads its config, and the dispatcher picks it up
automatically.

If a subclass is not on the classpath, `kms_adapter_from_config` raises
`LineageSignerError` at config-load time -- so a misconfigured
`bernstein.yaml` surfaces immediately rather than crashing on the first
audit-emit / lineage-sign call. For non-production smoke tests where
the silent-stub behaviour is acceptable, set
`BERNSTEIN_ALLOW_HSM_STUB=1` to opt in to the stub explicitly.

## Verifying a chain

A customer auditor with only the public key and the WAL files runs:

```python
from bernstein.core.persistence.lineage import (
    LineageReader,
    canonical_record_bytes,
    decode_signature,
)
from bernstein.core.persistence.lineage_signer import (
    Ed25519PublicKeyVerifier,
)

verifier = Ed25519PublicKeyVerifier.from_path(public_key_pem)
reader = LineageReader(sdd_dir)
for rec in reader.iter_records(run_id="r-2026-05-05"):
    if rec.customer_signature is None:
        continue  # v1 record, or signing was disabled
    sig = decode_signature(rec.customer_signature)
    assert verifier.verify(canonical_record_bytes(rec), sig)
```

## Producing a regulator artefact

```bash
bernstein lineage export r-2026-05-05 --format html --output /tmp/audit.html
bernstein lineage export r-2026-05-05 --format csv  --output /tmp/audit.csv
bernstein lineage export r-2026-05-05 --format jsonld --output /tmp/audit.jsonld
```

The HTML form is a single self-contained file (no JS, no external
assets) suitable for direct inclusion in a DORA / NIS2 evidence
package. The CSV form is ingestable by any GRC vendor that accepts
CSV. The JSON-LD form is shaped against schema.org `Action` so a
verifier with a JSON-LD library can graph-walk the chain.

## Tamper-loud detection

The janitor's lineage compaction step runs a chain verification
pass on every cycle. If verification fails the janitor:

1. Emits an `audit.jsonl` entry of type `lineage_tamper_detected`.
2. Increments `bernstein_lineage_tamper_total{run_id}`.
3. POSTs to the configured SIEM webhook (if any).

The janitor itself **does not block** on a tamper detection - it
records the event and lets the operator decide response policy via
the SIEM. Webhooks retry with exponential back-off on 5xx and fail
closed on a broken sink (the janitor never blocks on a bad webhook).

### Configuring the SIEM webhook

```yaml
tuning:
  lineage:
    alert_sink:
      kind: webhook
      url: https://siem.internal/bernstein-lineage-tamper
      headers:
        Authorization: "Bearer ${SIEM_TOKEN}"
      retries: 5
      backoff_seconds: [1, 2, 4, 8, 16]
```

For air-gap deployments the alternative `kind: syslog` writes to the
local syslog facility instead of HTTP.

### `bernstein lineage verify`

A one-shot chain verification that exits 0 only if every record's
HMAC and customer signature validate:

```bash
bernstein lineage verify r-2026-05-05
```

Useful for compliance teams running ad-hoc checks against archived
runs, or for CI gating against the most recent run.

## Out of scope

- Multi-key rotation registry.
- AI-generated regulatory-class inference.
- Direct integration with specific GRC vendor APIs (ServiceNow GRC,
  Archer, etc.). The exporter formats are generic; customers ingest
  CSV / JSON-LD / HTML through their existing pipeline.

## Limitations

- The customer signature covers full record bytes. Edits to any
  field invalidate the signature - including downstream-derived
  fields. This is intentional (simpler audit story); customers who
  need finer-grained signing can plug in a custom canonicaliser.
- Single signing key per run. Rotation across environments is on the
  operator (the schema accommodates a key id prefix in the signature
  blob; we do not ship a registry).
- Compliance metadata (`regulatory_class` vocabulary) is operator-
  supplied and unconstrained. We document a recommended set; we do
  not enforce it.

## Related

- Source: `src/bernstein/core/persistence/lineage.py`,
  `lineage_signer.py`, `core/observability/lineage_alert.py`
- CLI: `src/bernstein/cli/commands/{lineage_cmd,lineage_export_cmd,lineage_verify_cmd}.py`
- [Artifact lineage trail](../concepts/artifact-lineage.md) - schema reference and chain-walking concepts.
