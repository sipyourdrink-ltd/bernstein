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

`verify` is structural: it checks the schema version, that every element
carries a well-formed `sha256:` value, and that the deterministic ordering has
not been edited. Verifying the chain itself is `bernstein lineage verify <run>`.

## Determinism

Two derivations of the same run's BOM are byte-identical. The encoder is
canonical JSON (sorted keys, minimal separators, UTF-8) and every field is a
pure function of the spine, so a reviewer who re-runs the command against the
same chain gets the same bytes.
