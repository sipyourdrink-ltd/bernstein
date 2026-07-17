# Regulator-mapped compliance packs

The EU AI Act Article 12 bundle (`bernstein compliance pack article-12`) covers
record-keeping. Operators with obligations beyond record-keeping needed three
more evidence shapes, and used to assemble them by hand from raw exports. The
`bernstein compliance pack` group now ships them as one-command packs, each a
deterministic projection of the audit chain and each offline-verifiable with the
standalone verifier.

| Pack | Command | Maps to | Proves |
|------|---------|---------|--------|
| Retention | `pack retention --since --until` | Article 12(3) | logs existed over the window and were not truncated or rewritten |
| Incident | `pack incident --run <id>` | Article 73 | the incident timeline joined with its audit slice, evidence bundles, and receipts |
| Oversight | `pack oversight --since --until` | Article 14 | who approved what, and that the displayed action equalled the executed action |

Every pack is sealed with the same operator-key signing, signed SLSA-style
provenance manifest, and canonical-bytes rule (`PACK_FORMAT_VERSION` 2) as the
Article 12 bundle. Two builds over the same chain window yield byte-identical
member content hashes; build timestamps live only in the manifest, isolated from
the member hashes so the comparison is mechanical.

## Retention pack

```
bernstein compliance pack retention \
  --since 2026-01-01 --until 2026-12-31 \
  --org "Acme GmbH" --output acme-retention-2026.zip
```

Contents:

- `retention-evidence.json` records the boundary head hashes at the window
  edges, the entry count, detected coverage gaps, and the retention parameters
  in force.
- `lineage-log.jsonl` embeds the signed entries in canonical bytes, with their
  per-entry Ed25519 signatures under `signatures/` and Agent Cards under
  `agent-cards/`.
- `retention-evidence.pdf` and `.csv` are the human- and machine-readable views.

The verifier recomputes the boundary head hashes and entry count from the actual
signed entries, so the continuity claim is checked against the chain, not
trusted from the report.

## Incident pack

```
bernstein compliance pack incident --run run-42 \
  --org "Acme GmbH" --output acme-incident-run-42.zip
```

Reads `.sdd/incidents/<run>/timeline.json` (with optional `evidence_bundle_refs`
and `receipt_refs`) and `.sdd/incidents/<run>/audit-slice.jsonl`, then joins:

- `incident-timeline.json` and a fixed regulator-readable `incident-report.pdf` /
  `.csv`.
- `audit-slice.jsonl`, the prev_hmac-chained audit events in canonical bytes.
- `evidence-bundles/` and `receipts/`, the referenced artefacts present in the
  store.
- `gaps.json`, an explicit entry for every referenced artefact missing from the
  store.

The pack never fabricates completeness: a missing bundle or receipt is recorded
as a gap, and verification reports the gap list rather than passing silently.

## Oversight pack

```
bernstein compliance pack oversight \
  --since 2026-01-01 --until 2026-12-31 \
  --org "Acme GmbH" --output acme-oversight-2026.zip
```

Reads resolved approvals from `.sdd/approvals/resolved.jsonl` (or `--approvals`).
Each in-window approval becomes a canonical receipt under `receipts/` carrying
the attested displayed-versus-executed binding (`displayed_hash`,
`executed_hash`), the approving principal, and the decision outcome, summarised
in `oversight-evidence.json` and rendered in `oversight-report.pdf` / `.csv`.
The verifier recomputes each binding from the receipt payloads, so an auditor
confirms displayed equalled executed decision by decision.

## Verifying a pack offline

All three kinds verify on a machine with no Bernstein install:

```
pip install bernstein-verify
python -m bernstein_verify pack ./acme-retention-2026.zip
```

Exit 0 with a one-line `PASS` summary means every member's sha256 matches the
signed manifest `input_hashes`, the manifest self-anchors (`output_hash`), and
the chained substrate re-verifies for the pack kind. Flipping a single byte in
any member, including a rendered PDF, fails verification and names the member.
Existing Article 12 packs (format v1 and v2) verify unchanged.
