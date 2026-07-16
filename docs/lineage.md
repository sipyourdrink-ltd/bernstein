# Lineage v1 - user reference

Lineage v1 is Bernstein's per-artefact transparency log. Every agent write
to a tracked file produces an append-only, content-addressed, Ed25519-signed
entry that an external auditor can verify without any Bernstein install.

This page is the **user-facing** reference. For the design rationale see
[ADR-009](decisions/009-lineage-v1.md).

## TL;DR

| Question | Answer |
|---|---|
| What gets logged? | Every agent write to a tracked file or config. |
| Where does it live? | `.sdd/lineage/log.jsonl` (append-only) + per-entry detached JWS sidecars. |
| Who signs? | The agent itself, with an Ed25519 key issued at spawn time. |
| Who verifies? | Anyone with `bernstein-verify` (a standalone PyPI wheel). No Bernstein install required. |
| What gets shipped to auditors? | `bernstein compliance pack` - a single self-contained ZIP. |

## Quickstart

### Enable lineage

Lineage is on by default (since 1.10.9). To confirm:

```bash
bernstein config get lineage.enabled
# true
```

By default `lineage.strict` is `false`, so lineage failures warn but don't
block writes:

```bash
bernstein config get lineage.strict
# false
```

Once your team is confident, flip to strict:

```bash
bernstein config set lineage.strict true
```

### View the log

```bash
# Full chain for one artefact
bernstein lineage chain src/payments/flow.py

# Walk the ancestry of one artefact
bernstein lineage walk src/payments/flow.py

# Active forks (none should exist on a clean main)
bernstein lineage forks
```

### Generate a compliance pack

```bash
bernstein compliance pack \
  --since 2026-01-01 --until 2026-03-31 \
  --org "Acme Bank" \
  --output ./acme-q1-evidence.zip
```

Pack contents:

| File | Purpose |
|---|---|
| `README.md` | Cover page. |
| `article12-evidence.csv` | Machine-readable: every artefact write, agent, ts, content hash. |
| `lineage-log.jsonl` | Raw log slice for re-verification. |
| `signatures/` | Per-entry detached JWS. |
| `agent-cards/` | A2A v1.0 Agent Cards seen during the window. |
| `pack-manifest.json` | Provenance: who packed, when, hashes. Operator-signed. |
| `verify-instructions.md` | One-pager for the auditor. |

## Auditor flow

The auditor never has to install Bernstein. They install one tool:

```bash
pipx install bernstein-verify
```

Then point it at the pack:

```bash
bernstein-verify pack ./acme-q1-evidence.zip
```

Exit code semantics:

| Exit | Meaning |
|---|---|
| 0 | All signatures valid, chain complete, no unresolved forks. Pack PASSES. |
| 1 | Any failure. Human summary on stdout, structured JSON on stderr. |

Sub-commands:

| Command | Purpose |
|---|---|
| `bernstein-verify pack <zip>` | Full pack verification end-to-end. |
| `bernstein-verify chain <path> --lineage-dir DIR` | Single artefact chain. |
| `bernstein-verify forks <path> --lineage-dir DIR` | Report unresolved forks (CI use). |

The verifier is air-gap-safe - no network calls, no remote registry
lookups. Every public key it needs is in `agent-cards/` inside the pack.

## Article 12 paragraph mapping reference

EU Regulation 2024/1689 Article 12 paragraph numbers → what in the pack
satisfies them.

| Paragraph | Obligation | Pack artefact | Pack location |
|---|---|---|---|
| 12(1) | Automatic recording over lifetime. | The lineage log itself. | `lineage-log.jsonl` |
| 12(2)(a) | Traceability appropriate to intended purpose. | Each entry carries `artefact_path`, `content_hash`, `agent_id`, `ts_ns`. | `lineage-log.jsonl` + `signatures/` |
| 12(2)(b) | Identification of risk situations / substantial modifications. | Entries whose narrative changes risk parameters. | `article12-evidence.csv` (filter on artefact_path) |
| 12(2)(c) | Post-market monitoring (re. Article 72). | Cross-link via `tool_call_id` to audit log. | `lineage-log.jsonl` (tool_call_id column) |
| 12(2)(d) | Monitoring per Article 14(3) human oversight. | Reviewer-agent entries. | `lineage-log.jsonl` (filter on agent_id) |
| 12(3) | At least 6-month retention. | Pack itself is a retainable artefact; cold-storage path documented. | Pack ZIP + retention chain. |

For Annex IV (technical documentation) and Article 11 (technical
documentation obligations), see the healthcare demo:
[`examples/lineage/healthcare/article12-mapping.md`](https://github.com/sipyourdrink-ltd/bernstein/blob/main/examples/lineage/healthcare/article12-mapping.md).

For the 10-year cold-storage round-trip path used in industrial-machinery
contexts:
[`examples/lineage/eu-manufacturer/cold-storage-roundtrip.md`](https://github.com/sipyourdrink-ltd/bernstein/blob/main/examples/lineage/eu-manufacturer/cold-storage-roundtrip.md).

## Demo bundles

Three reference scenarios ship under `examples/lineage/`:

| Demo | Story | Path |
|---|---|---|
| Fintech | SOC2 + 4 agents editing payments code; rogue parallel-edit detection. | `examples/lineage/fintech/` |
| Healthcare | HIPAA + EU AI Act Article 11 + 12 over a triage config. | `examples/lineage/healthcare/` |
| EU manufacturer | Annex III high-risk machinery; 10-year cold-storage round-trip. | `examples/lineage/eu-manufacturer/` |

Run any of them:

```bash
make -C examples/lineage demo-fintech
make -C examples/lineage demo-healthcare
make -C examples/lineage demo-eu-mfg
```

## Failure modes you should know

| Symptom | Likely cause | Action |
|---|---|---|
| `bernstein-verify pack` exits 1 with "fork detected" | Two agents wrote the same artefact in parallel without a Steward merge. | Run `bernstein lineage merge <path>` to record a merge entry; re-pack. |
| `verify` exits 1 with "invalid signature" | The log was edited after the fact, or the Agent Card was swapped. | Treat as a security incident; do not ship the pack. |
| `verify` exits 1 with "kid binding cannot be established" | No Agent Card on disk matches the `(agent_id, agent_card_kid)` the entry signed - typically a card for the entry's key id is missing after a rotation. | Restore the card for that key id under the per-kid layout (see [Key rotation](#key-rotation)); re-verify. |
| `verify` exits 1 with "kid binding mismatch" | An entry's signed body names one key id while its JWS header names another - a key-substitution attempt. | Treat as a security incident; do not ship the pack. |
| `verify` exits 1 with "HMAC mismatch" | Operator HMAC key rotated mid-window. | Re-pack with the correct key context; consult ADR-009 §6. |
| `bernstein lineage gate` reports "non-canonical line bytes" | A `log.jsonl` line's raw bytes differ from the canonical form the writer emits - reordered keys, inserted whitespace, or a stray `\r`. A generic JSONL tool or a hand-edit that pretty-printed or reformatted the log triggers this even when the field values are unchanged. The gate binds verification to the on-disk bytes, so a non-canonical rewrite is rejected rather than silently re-canonicalised. | The log is the provenance anchor and is never edited in place by Bernstein. Restore `log.jsonl` from a trusted copy and re-run; if no trusted copy exists, treat as a tamper incident. |
| `bernstein lineage gate` reports "missing trailing newline" | The final record was truncated or its terminating `\n` was stripped/flipped at EOF (e.g. an editor that drops the trailing newline, or a partial write). | Restore the full log from a trusted copy; a truncated final record is tamper-evidence. |
| Genesis entry shows up with `parent_hashes: []` | First-time write of a file that existed before lineage was enabled. | Expected - see ADR-009 §11 on bootstrap. |

## Key rotation

Each lineage entry signs the key id (`agent_card_kid`) it was produced under, and the gate verifies the signature against the Agent Card for that exact `(agent_id, kid)` pair - not against whatever card currently sits at the agent id. This keeps historical entries verifiable after an agent rotates its key.

The gate reads two on-disk Agent Card layouts:

| Layout | Path | Use |
|---|---|---|
| Single-card | `<cards-dir>/<agent-id>/card.json` | One key per agent (default). |
| Per-kid | `<cards-dir>/<agent-id>/<kid>/card.json` | Multiple historical keys for one agent, one card per key id. |

To rotate a key without invalidating prior entries, keep the old card and add the new one under the per-kid layout:

```
.sdd/agents/
  agent:claude-worker-3/
    k-2025-01/card.json   # old key - retained so old entries still verify
    k-2025-06/card.json   # new key - signs entries from the rotation onward
```

Entries signed under `k-2025-01` continue to verify against the retained card; new entries under `k-2025-06` verify against the new one. Removing a card for a key id that historical entries still reference makes those entries fail the gate with a kid-binding error.

## See also

- [ADR-009: Lineage v1](decisions/009-lineage-v1.md) - design rationale, schema, threat model.
- [Compliance - EU AI Act Article 12 bundle](compliance/eu-ai-act-article-12-bundle.md) - the bundle format detail.
- [Regulatory lineage export](compliance/lineage-export.md) - operator export guide.
- A2A v1.0 Agent Card spec; RFC 7515 JWS; RFC 8785 JCS.
