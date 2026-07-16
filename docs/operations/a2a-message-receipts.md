# Signed A2A message receipts

Bernstein already signs A2A capability cards, but the cross-agent task messages
themselves were not attestable, so a reviewer could not prove a given
cross-agent call happened with the exact inputs claimed. Every inbound/outbound
[A2A v1.0](https://a2a-protocol.org/) message is now a signed lineage receipt
anchored to the message-receipt spine, and each inbound peer task runs in its
own git worktree so no two collaborations share mutable state.

## What it gives you

| Field | Binds |
|---|---|
| `message_hash` | task uuid + direction + state + seq + body hash |
| `peer_card_fingerprint` | `sha256:` fingerprint of the peer's verified card key |
| `task_uuid` | the A2A task (the journal trace root) |
| `journal_entry_hash` | the message-receipt spine entry anchoring the receipt |

Each receipt is signed with the install's Ed25519 identity and anchored in the
message-receipt lineage spine. The message body itself is never stored on the
receipt or in the audit chain, only its hash.

## Task lifecycle -> journal terminal states

The A2A task lifecycle maps 1:1 to journal states with reason codes, keyed by
`task_uuid` as the trace root:

| A2A state | Journal state | Default reason | Terminal |
|---|---|---|---|
| `submitted` | `task_submitted` | `received` | no |
| `working` | `task_working` | `in_progress` | no |
| `input-required` | `task_input_required` | `awaiting_input` | no |
| `completed` | `task_completed` | `success` | yes |
| `failed` | `task_failed` | `error` | yes |
| `canceled` | `task_canceled` | `canceled` | yes |

An unknown A2A state is rejected rather than silently mapped.

## Inbound trust check

Before a peer task is accepted, its capability card is verified against its
declared issuer domain and cross-checked against the operator's trusted-issuer
set. `accept_inbound_task` rejects, with a distinct reason code, a card whose:

- signature is invalid or expired (`signature`);
- `issuer` does not match the message origin (`issuer_mismatch`);
- key fingerprint is not in the trusted set (`untrusted_issuer`);
- advertised policies do not meet the operator's requirements (`policy`).

## Worktree isolation

Each inbound peer task runs in a dedicated git worktree keyed by a session id
derived from `task_uuid` (see `adapters/session_id.py`). The worktree's isolation is
validated so its `.sdd` is a real per-worktree directory, not a symlink into the
parent repo, so a peer cannot clobber another peer's state.

## Verify

```
bernstein interop a2a verify-thread --from-thread <task-uuid>
```

For the task uuid, recomputes every message receipt binding, re-checks each
Ed25519 signature offline against the receipt's embedded public key, verifies
the message-receipt spine, re-anchors each receipt against it, and confirms
every message hash is referenced by the seeded per-task journal.

| Exit code | Meaning |
|---|---|
| 0 | every message in the thread verifies against the spine and the journal |
| 1 | no receipts for the thread, or a tampered receipt / spine / journal |

Add `--json` for machine-readable output.

## Where receipts live

```
.sdd/a2a-messages/<task>/<seq>.json         # signed message receipt
.sdd/lineage/a2a-messages/spine.jsonl       # anchoring spine (Merkle + HMAC)
.sdd/runs/a2a-<hash>-<task>/journal.jsonl   # seeded per-task journal
```

Every receipt is mirrored into the HMAC-chained audit log as an
`a2a.message_receipt` event carrying only the hashes, the peer fingerprint, the
task uuid, and the lifecycle state, so an operator can prove from the chain
alone that a cross-agent call happened with the exact inputs claimed.
