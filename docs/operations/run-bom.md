# AI bill of materials

`bernstein bom` projects an AI bill of materials — the inventory of what a run
depended on — out of state the run already produced. It records nothing new: a
BOM is a projection over the lineage spine, so it can be re-derived at any time
and any line item resolves back into the chain.

## Emit a BOM from a run's lineage chain

```bash
bernstein bom emit --run 20260101-104501 --from-lineage
```

`--from-lineage` walks `.sdd/lineage/<run>/spine.jsonl` and builds the document
from it. Without the flag, `--run` reads a hand-assembled
`.sdd/runs/<run>/bom_snapshot.json` instead — useful when a snapshot comes from
another system, but nothing in Bernstein writes that file.

`--format` accepts `json` (default), `cyclonedx` and `spdx`; `--out <path>`
writes to a file instead of stdout.

## What the projection carries

| Field | Drawn from |
|---|---|
| `run_id` | the spine run directory |
| `started_at` / `finished_at` | earliest and latest spine entry timestamp |
| `lineage_root_hash` | the chain head — the run's provenance identity |
| `models[].name` | the `model` string recorded on the spine entry |
| `models[].invocation_count` | number of spine entries naming that model |
| `models[].sha256` | `entry_hash` of the first spine entry naming that model |

Each model's `sha256` is a lineage entry hash, so a reviewer can resolve a line
item against `bernstein lineage replay <run>` rather than taking it on trust.

Spine entries that recorded no model — the shape every non-model artifact write
uses — contribute no component: an artifact write that named no model did not
invoke one.

`provider` and `version` are empty for a lineage-derived BOM. The spine records
the model string only, and splitting a provider out of it would be a fresh
claim rather than a projection.

`prompts`, `adapters`, `tools` and `data_sources` are empty for a
lineage-derived BOM today; the spine does not carry those component classes.

## Requirements and failure modes

The spine is HMAC-tagged, so `--from-lineage` loads the audit key the chain was
written under (`$BERNSTEIN_AUDIT_KEY_PATH`, else the XDG state key). It is
loaded read-only and never minted: a freshly generated key cannot authenticate
an existing chain, so emitting under one would produce a document off a chain
this install cannot vouch for.

| Situation | Result |
|---|---|
| Run has no spine entries | exit 1, names the run and the spine path |
| Audit key missing or world-readable | exit 1, names the key problem |
| `--from-lineage` without `--run` | exit 2 |
| `--from-lineage` with `--snapshot` | exit 2 |

## Verify a BOM document

```bash
bernstein bom verify ./bom.json
```

`verify` without `--from-lineage` is structural only: it checks the schema
version, that every element carries a well-formed `sha256:` value, and that the
deterministic ordering has not been edited. It never reads the lineage spine,
so it cannot tell a faithful document from an edited one, and it says so on
PASS. `--run` and `--workdir` are rejected (exit 2) when `--from-lineage` is
not given.

To confirm the document is a faithful projection of what actually ran, pass
`--from-lineage` with the run id:

```bash
bernstein bom verify ./bom.json --from-lineage --run 20260101-104501
```

This recomputes the BOM offline from `.sdd/lineage/<run>/` and compares it with
the document field by field. It fails closed when:

- the chain does not verify (a tampered, deleted or reordered entry, a bad
  HMAC tag, an empty or seal-only run);
- `run_id`, the run window or `lineage_root_hash` differ from the chain;
- a component is missing, extra or renamed;
- a component's `sha256`, `invocation_count` or any other field differs;
- the document lists prompts, adapters, tools or data sources, which the spine
  does not record and so cannot be compared with.

Each failure names the offending line item. A BOM emitted from a hand-written
`--snapshot` therefore does not verify against lineage. The audit key the chain
was written under is loaded read-only and never minted.

## Determinism

Two derivations of the same run's BOM are byte-identical. The encoder is
canonical JSON (sorted keys, minimal separators, UTF-8) and every field is a
pure function of the spine, so a reviewer who re-runs the command against the
same chain gets the same bytes.

## CycloneDX version and the ML-BOM model card

`--format cyclonedx` emits **CycloneDX 1.7** (`$schema` is
`http://cyclonedx.org/schema/bom-1.7.schema.json`). The version is pinned in
the encoder rather than resolved at run time, because a compliance document
has to name the specification its bytes validate against. The dependency SBOM
(`core/security/sbom.py`) and the per-run compliance SBOM
(`core/security/compliance.py`) emit the same version: one release does not
ship two CycloneDX versions. The vendored copy of
the official schema lives in `tests/fixtures/cyclonedx/` (provenance and
digests in its README), and
`tests/unit/compliance/test_ai_bom_cyclonedx_schema.py` validates the emitted
document against it offline.

### Model card fields that are emitted

| `modelCard` field | Drawn from |
|---|---|
| `modelParameters.modelArchitecture` | the model identifier recorded on the spine (`models[].name`) |

The specification asks `modelArchitecture` for the specific model -- its own
examples are `GPT-1`, `ResNet-50`, `YOLOv3` -- and the identifier the run
records is exactly that fact. No classification of the string takes place.

### Model card fields that are deliberately absent

A BOM is a projection: every line item has to resolve back into the chain, so
a field with no recorded fact behind it is omitted rather than inferred.

| Field | Why it is absent |
|---|---|
| `modelParameters.task`, `architectureFamily`, `approach` | the run records the model identifier, not the model's ML task, architecture family or learning approach. Filling these means classifying every model string against a taxonomy the chain cannot prove |
| `modelParameters.datasets` | the run does not record which dataset trained the model. Data sources the run *read* stay as separate `data` components; calling them training data would be a different, unverifiable claim |
| `quantitativeAnalysis.performanceMetrics` | evaluation results are recorded against gates and eval suites, not against a model entry, so there is no per-model metric to project |
| provider endpoint | the spine records the model string only; no endpoint URL is recorded anywhere in a run, so none is emitted. `component.publisher` carries the provider when the snapshot supplies one |

`provider` and `version` are empty for a lineage-derived BOM and carry the
snapshot's values for a hand-assembled one -- see the projection table above.

`serialNumber` is a UUID URN derived deterministically from the run id
(`uuid5` in the AI-BOM schema namespace). CycloneDX requires
`urn:uuid:<uuid>`, and the readable run id stays in
`metadata.properties[bernstein:run_id]`, so nothing is lost against the
previous, schema-invalid form.

## SPDX: staying on 2.3 for now

Decision (2026-09): the SPDX encoder stays on **SPDX 2.3**; no SPDX 3.0
AI-profile output is added yet. Reasons, in order:

1. No consumer has asked for it. The procurement checks behind the CycloneDX
   move name the CycloneDX ML-BOM fields; SPDX 3.0 appears in those
   questionnaires only as an alternative, and no questionnaire we can point at
   requires the 3.0 shape specifically.
2. SPDX 3.0 is a different document model, not a version bump. It is
   JSON-LD with `@context`/`spdxId` and a separate AI profile, so the cross-walk
   would have to be re-derived field by field against a second official schema,
   vendored the same way -- a second maintenance surface for no requested
   capability.
3. 2.3 is what the SBOM scanners Bernstein drives ingest. Emitting 3.0 alone
   risks a document the consumer cannot read, which is worse than a 2.3
   document that still carries the full model and package inventory.

Revisit when a named requirement asks for the SPDX 3.0 AI profile, or when the
SPDX project deprecates the 2.x line. The decision is recorded here, next to
the projection tables, rather than in a code comment.
