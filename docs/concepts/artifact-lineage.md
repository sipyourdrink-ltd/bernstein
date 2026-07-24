# Artifact lineage trail

Every write an agent produces is recorded as a `LineageRecord` linking
the output back to the producing prompt, the input artefacts the agent
read, the model, the run, and the cost. The chain is HMAC-signed and
artefact-indexed, so "which agent run, which prompt, which source
files produced this broken line?" becomes a one-command lookup.

This page covers the base schema. Customer-key signing and
regulator-class fields are documented in
[Regulator-class lineage](../compliance/regulatory-lineage.md).

## Why it exists

The HMAC audit log is event-ordered: "agent X wrote file Y at time T."
That is enough for forensics, not enough for compliance. EU AI Act,
DORA, and SOC2 audits ask "show me the chain for this artefact" -
producing prompt, input bytes, model, cost. Lineage is that chain.

It is also the tool we reach for when:

- Cross-model verifier flags a divergence and we need to see which
  prompt + which input file produced it.
- A regression lands and we need to bisect by producer.
- We want to attribute tokens / cost back to the originating task.

## How to use it

Lineage records are emitted automatically by the WAL writer on every
`apply_patch`-style tool call. There is nothing to enable for the
write side. To read the chain back, use the `lineage` CLI:

```bash
# Walk the chain for one file (or one line within it)
bernstein lineage src/foo.py
bernstein lineage src/foo.py:42

# Filter by run
bernstein lineage src/foo.py --run r-2026-05-05

# Export for a regulator (HTML / CSV / JSON-LD)
bernstein lineage export r-2026-05-05 --format html  --output /tmp/audit.html
bernstein lineage export r-2026-05-05 --format csv   --output /tmp/audit.csv
bernstein lineage export r-2026-05-05 --format jsonld --output /tmp/audit.jsonld

# Re-verify the HMAC + customer-key chain
bernstein lineage verify r-2026-05-05
```

The chain walks output → producing prompt → input artefact → upstream
producer recursively. CLI text output prints the most recent producer
first; `--limit` caps how many records are shown.

### Coverage and the `SEAL_ONLY` verify status

Per-artifact records are captured at the in-process write boundary
(`record_artifact_write`). CLI adapters (qwen, claude, ...) spawn a
subprocess that writes files directly on disk; those writes do not cross
that boundary, so a CLI-adapter run's spine may contain only the
journal-head seal and no produced-artifact record. `bernstein lineage
verify` reports that case as the distinct **`SEAL_ONLY`** status with a
non-zero exit, rather than a clean `OK`, so a chain with no artifact
provenance is never mistaken for "provenance confirmed".

### The audit key is required (verify never mints one)

Each spine entry is HMAC-tagged with the install's audit key. `verify` only
reads the chain, so it loads that key **read-only** and never creates one: a
freshly minted key cannot authenticate an existing chain, so every tag would
fail and a plain missing-key setup error would be misreported as tamper. When
no key is found, `verify` fails closed with a clear "key missing" message and
exits `3` (distinct from `2` = tamper).

The key is resolved from `$BERNSTEIN_AUDIT_KEY_PATH` or the XDG state path. An
auditor verifying a handed-over evidence package points `--key-path` at the key
the chain was written under:

```bash
bernstein lineage verify <run_id> --key-path ./handover/audit.key
```

Exit codes: `0` OK, `1` no entries / seal-only, `2` tamper detected,
`3` cannot verify (audit key missing).

## Programmatic access

```python
from pathlib import Path
from bernstein.core.persistence.lineage import LineageReader

reader = LineageReader(sdd_dir=Path(".sdd"))
# iter_records optionally filters by run_id; filter on the artefact
# path yourself when you only want one file's chain.
for record in reader.iter_records(run_id="r-2026-05-05"):
    if record.output_artifact.path == "src/foo.py":
        print(record.producer.agent_id, record.prompt_sha, record.cost_usd)
```

Each `LineageRecord` carries:

- `output_artifact` - `path`, `sha256`, byte / line range
- `inputs` - list of `ArtifactRef`
- `producer` - `agent_id`, `run_id`, `tick_id`
- `prompt_sha`, `model`, `cost_usd`, `tokens`, `timestamp`
- `regulatory_class`, `customer_signature` (only populated when
  customer-key signing is enabled; see
  [Regulator-class lineage](../compliance/regulatory-lineage.md))

## Configuration

| Knob | Default | Controls |
|---|--:|---|
| `lineage.enabled` | `true` | Emit records on every write. |
| `lineage.compaction.enabled` | `true` | Janitor gzips per-day files at compaction time. |
| `lineage.regulatory_class.default` | `null` | Pin a default regulatory class for the run. |
| `lineage.customer_signing.*` | see [regulator doc](../compliance/regulatory-lineage.md) | Customer-key signing knobs. |

`bernstein debug bundle` includes the lineage graph for the run.

## Limitations

- Single-run scope. Cross-run stitching is operator-driven (export the
  per-run records, join externally).
- No backfill. Historical writes from before the feature was enabled
  have no records.
- CLI text and HTML/CSV/JSON-LD exporters; no GUI.
- PII redaction lives in `core/security/pii_output_gate.py`; lineage
  records inherit whatever redaction the audit log already applies -
  no extra layer.

## Related

- Source: `src/bernstein/core/persistence/lineage.py`
- CLI: `src/bernstein/cli/commands/lineage_cmd.py`,
  `lineage_export_cmd.py`, `lineage_verify_cmd.py`
- [Regulator-class lineage](../compliance/regulatory-lineage.md) - regulatory class, customer signature, tamper-loud surface
- PRs #996, #1013, #1017
