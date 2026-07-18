# Mission timeline and signed daily progress digests

A **mission** is a multi-day goal decomposed into ordered phases, each with a
verification gate and its own budget envelope. The order fixes how phases are
declared and reported, not a single-file execution lock: envelopes are
per-phase, so one phase halting on an exhausted envelope leaves its siblings
runnable. Mission status is never stored: it is a pure deterministic projection
over the mission's work-ledger chain plus the evidence bundles its phase
receipts reference (see the [work ledger](work-ledger.md) and the review
board's projection discipline).

Two surfaces render that projection so operators do not have to reverse-engineer
overnight progress from task lists and logs.

## Mission timeline screen

The web UI route `Missions` renders the deterministic mission projection:

- **Phase lanes** with per-phase state (`pending` / `active` / `passed` /
  `halted` / `unverified`), gate verdict, and per-phase envelope burn.
- **Provenance on every element.** Each phase links to the gate receipt hash it
  advanced by and to the sealed evidence bundle each gate task verified. No
  element renders without a receipt-, ledger-, or evidence-backed origin.
- **Status hash.** The screen shows the `mission_status_hash` so two operators
  can confirm they are looking at the same folded state.
- **Unverified banner.** When the work-ledger chain no longer recomputes (a
  tampered entry) or a referenced evidence bundle no longer matches the hash its
  receipt bound, the screen switches to an explicit unverified banner instead of
  best-effort rendering.
- **Gate binding.** A phase renders `passed` only when its receipt binds the
  exact gate the phase declares: a passing verdict, precisely the gate's task
  ids, one bundle hash per task id, a receipt hash that agrees with its own
  contents, and bundles that still hash as recorded. A receipt that verifies
  unrelated evidence, or binds none at all, renders `unverified`. A ledger
  declaring more than one mission renders `unverified` outright, since the
  projection and the evidence lookup could otherwise read different specs.
- **Phase isolation.** Phases run under isolated envelopes, so a halted phase
  is terminal for itself alone. Sibling phases keep running, the reported
  active phase is the first one still runnable, and the mission reports
  `halted` only once no phase remains runnable. These rules landed with
  `schema_version` 2 of the status projection: a ledger carrying a halt beside
  a runnable phase folds to a different `mission_status_hash` than it did under
  version 1. The version travels inside the hashed status, so a verifier
  holding a pre-upgrade digest can tell a rules change from a tampered chain.
  Digests computed under version 1 do not re-verify against a version 2
  projection of the same ledger; recompute them from the ledger.

The read-only projection routes back this screen:

| Route | Returns |
| --- | --- |
| `GET /missions` | mission ids with a ledger, newest first |
| `GET /missions/{mission_id}` | the projection receipt: canonical status, `mission_status_hash`, `ledger_verified`, `evidence_verified` |
| `GET /missions/{mission_id}/digest?fire_time=<epoch>` | the canonical digest for a fire instant, its `digest_hash` / `receipt_id`, and the verbatim chat message |
| `GET /missions/{mission_id}/evidence/{task_id}` | the sealed evidence bundle behind a phase gate |

The server holds no mission-side state; the same ledger bytes serve the same
`mission_status_hash` from any host.

## Signed daily progress digest

A recurring fire computes a canonical **digest** from the mission projection at a
fire instant: phases advanced, gates passed or failed, spend per envelope, and
blockers. The digest is a pure fold of `(mission projection, fire time)`, so two
operators holding byte-identical ledgers, folding at the same `fire_time`, derive
byte-identical digest bytes and the same `digest_hash`. A missed fire recomputes
to the identical digest after a restart.

The digest is not a status message with a hash bolted on: the posted chat message
is the verbatim projection of the digest, the `digest_hash` is embedded in the
message and recorded in the HMAC audit chain as a `mission.digest_receipt`, and a
verifier recomputes the digest from the ledger to prove the posted message matches
the chain-attested receipt. An edited or truncated message is detected as a
mismatch; a tampered receipt fails chain verification at its exact position.

Delivery is idempotent on the digest receipt id (a pure function of
`mission_id`, `fire_time`, and `digest_hash`): a restart between fire computation
and chat delivery does not double-post. Delivery travels only over the common
chat bridge `send_message` path, so it works uniformly across the Slack, Discord,
and Telegram drivers.

### CLI

```bash
# Print the canonical digest for a fire instant (read-only, no chain write).
bernstein mission digest show <mission_id> --fire-time <epoch>

# Record the digest receipt and post it to chat, idempotent per fire.
bernstein mission digest send <mission_id> --fire-time <epoch> \
    --platform slack --thread <channel>

# Recompute the digest from the ledger and prove a posted message matches it.
bernstein mission digest verify <mission_id> --fire-time <epoch> \
    --message posted-message.txt
```

`verify` exits non-zero when the posted message does not match the
ledger-recomputed digest, when the audit chain does not verify, or when no chain
receipt binds the recomputed `digest_hash`.
