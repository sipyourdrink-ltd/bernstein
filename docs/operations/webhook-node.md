# Audited webhook node

Drop Bernstein into a no-code builder or event bus as the one node that turns an
opaque flow step into a verifiable one. An inbound webhook triggers a run; both
the inbound event and the outbound result carry signed receipts anchored to the
run journal and the webhook-node lineage spine.

## What it gives you

| Direction | Receipt binds | Anchored in |
|---|---|---|
| Inbound | `{event_hash, source, journal_root}` | seeded run journal + webhook-node spine |
| Outbound | `{result_hash, journal_head}` | webhook-node spine |

- **Standard Webhooks** signatures on both directions (`webhook-id` /
  `webhook-timestamp` / `webhook-signature`, `v1,<base64>` HMAC-SHA256 over
  `{id}.{timestamp}.{body}`). Multiple space-separated candidate signatures are
  accepted so a sender rotating secrets is not rejected.
- **Idempotent inbound**: a retry/backoff replay of the same `webhook-id`
  returns the recorded receipt and does not spawn a second run.
- **Rejected on bad signature**: an inbound event whose signature does not
  verify writes nothing and spawns nothing.

## Verify

```
bernstein webhook verify <event_id>
```

Recomputes the inbound event hash and the outbound result hash against the run
journal, re-checks both Ed25519 signatures offline against the receipts'
embedded public keys, and re-anchors both receipts against the webhook-node
lineage spine.

| Exit code | Meaning |
|---|---|
| 0 | inbound and outbound receipts verify against the journal |
| 1 | no inbound receipt, or the run has not produced an outbound receipt yet |
| 2 | tamper: a receipt, the spine, or the journal no longer recomputes |

## Where receipts live

```
.sdd/webhook-node/inbound/<event_id>.json     # signed inbound receipt
.sdd/webhook-node/outbound/<event_id>.json    # signed outbound receipt
.sdd/lineage/webhook-node/spine.jsonl         # anchoring spine (Merkle + HMAC)
.sdd/runs/webhook-<hash>/journal.jsonl        # seeded run journal
```

Every receipt is mirrored into the HMAC-chained audit log as a
`webhook_node.receipt` event carrying only hashes and the source label -- never
the webhook body -- so an operator can reconstruct, from the chain alone, that a
no-code step ran under a signed inbound event and returned a signed outbound
result.
