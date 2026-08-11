# Trajectory receipts: portable, offline-verifiable benchmark scores

> A published benchmark score that ships without a replayable receipt is
> indistinguishable from a hand-typed number.

---

## Overview

Every number produced by `bernstein benchmark` now ships as a **trajectory
receipt**: a content-addressed, spine-anchored, offline-verifiable envelope
that lets any third party re-derive the score from the embedded trajectory
without re-running the suite, without access to the operator's machine, and
— for the Ed25519/COSE path — without any HMAC key.

This closes three failure modes that were previously undetectable from a bare
scalar alone:

| Failure mode | How the receipt catches it |
|---|---|
| **Cherry-picking** | A best-of-N publication must carry *all N candidate journal heads* and the deterministic selection rule. Any receipt carrying only the winner is rejected as unverifiable. |
| **Contamination** | The receipt seals a `suite_content_hash` over the exact golden task set. A quietly mutated task changes the hash; verification recomputes it and rejects the mismatch. |
| **Fabrication** | The aggregate score is not trusted — it is re-derived from the per-task `EvalScoreComponents` via the harness formula. A hand-typed scalar that disagrees with the re-derivation fails step 4. |

---

## Quick start

```bash
# After running a benchmark suite:
bernstein benchmark receipt emit <run_id>
# → prints: sha256:<receipt_hash>

# Verify offline (you + any third party with access to .sdd/):
bernstein benchmark receipt verify sha256:<receipt_hash>

# Verify as part of the full audit pillar sweep:
bernstein audit verify
```

---

## CLI reference

### `bernstein benchmark receipt emit <run_id>`

Seals the completed benchmark run into a signed trajectory receipt and writes
it under `.sdd/eval/bench/sha256:<hash>.json`.

The receipt commits to:

- **Suite identity** (`suite_content_hash`) — SHA-256 over the ordered task-id
  set. Two runs on the same hash provably ran the same task set.
- **Per-task trajectory anchors** — for each task: the `EventJournal` head
  hash, the `ReplayGateway` fixture (`events.jsonl`) content hash, the model id,
  and the config fingerprint.
- **Scoring evidence** — the per-task `EvalScoreComponents` and the aggregate
  `TierScores`, so verification re-derives rather than trusts the printed number.
- **Selection provenance** — for best-of-N runs, the journal heads of *all N
  candidates* and the deterministic selection rule.

No timestamp or wall-clock value enters the signed bytes. The receipt hash is
therefore byte-identical across independent recordings of the same run against
the same fixtures.

**Example output:**

```
Trajectory receipt emitted: sha256:c60eb55038d7272e770de80f4e3f...
  Tasks:  12
  Score:  0.9312
  Status: single-shot
```

### `bernstein benchmark receipt verify <receipt_hash>`

Verifies the receipt offline. Verification re-derives; it never trusts stored
values. Every step must pass or the command exits non-zero:

1. **Byte integrity** — the stored file is the canonical encoding of the receipt
   it decodes to; the hash covers the raw bytes, not a parsed projection.
2. **Hash recompute** — `receipt_hash` recomputes from the receipt body.
3. **Contamination check** — `suite_content_hash` recomputes from the embedded
   task ids.
4. **Aggregate re-derivation** — `EvalScoreComponents` re-derive from the
   per-task anchors via `Score = (0.5·TaskSuccess + 0.3·CodeQuality +
   0.2·Efficiency) · Reliability · Safety`.
5. **Scalar check** — `published_score` matches the recomputed `final_score`.
6. **Selection provenance** — for best-of-N, all N candidate heads are present
   and the re-selected index matches the published one.
7. **Spine anchor** — the receipt's canonical bytes are anchored in the
   `eval-bench` `LineageSpine`; a receipt not in the spine is rejected.

**Exit codes:** `0` = verified clean, `1` = verification failed (reason
printed), `2` = usage error.

**Example output (clean):**

```
✓ Trajectory receipt verified: sha256:c60eb55038d7272e770de80f4e3f…
  Tasks:       12
  Score:       0.9312
  Status:      single-shot
  Suite hash:  sha256:7f3a…
```

**Example output (tampered scalar):**

```
✗ Trajectory receipt FAILED: sha256:c60eb55038d7272e770de80f4e3f…
  published_score 9.99 does not match recomputed final_score 0.9312 (scalar edit)
```

---

## `bernstein audit verify` integration

`bernstein audit verify` sweeps every integrity pillar in sequence.
Trajectory receipts are one pillar, alongside HMAC chain, Merkle seal,
evidence bundles, and tournament selection receipts.

The contract mirrors every other pillar exactly:

- **Absence** — no `.sdd/eval/bench/` directory, or an empty one, is a **silent
  no-op**. The check returns `True` immediately. A project that does not use
  `bernstein benchmark receipt emit` is not penalised.
- **Presence + intact** — all receipts pass → the pillar prints a green
  confirmation panel and returns `True`.
- **Presence + tampered** — any receipt whose re-derivation fails → the pillar
  prints the receipt hash and the exact failure reason, and **hard-fails**
  (`audit verify` exits `1`). There is no "warning" mode. A score that cannot
  be verified from its trajectory is treated as unverifiable, not merely
  suspicious.

---

## Offline third-party verification (no HMAC key)

The HMAC audit chain is symmetric — only the operator who holds the HMAC key
can check it. For external reviewers (auditors, leaderboard verifiers, paper
reviewers) who hold *no key*, the receipt head is projected into three
portable envelope formats via `trajectory_receipt_projection.py` (#2925 PR 2):

| Format | Standard | File extension |
|---|---|---|
| COSE_Sign1 | RFC 9052 + RFC 9053 (EdDSA, alg=-8) | `.cose` |
| in-toto DSSE | in-toto Envelope v1 + SLSA Provenance | `.intoto.json` |
| Transparency leaf | RFC 6962 signed tree head | `.rfc6962.leaf` |

All three envelopes sign the same subject: the `sha256:`-prefixed receipt hash.
An external reviewer:

1. Verifies the COSE (or DSSE, or transparency) envelope against the operator's
   published Ed25519 public key — confirms the receipt hash without any HMAC
   key.
2. Fetches the receipt JSON by that hash from the evidence store.
3. Runs `bernstein benchmark receipt verify <hash>` — re-derives the score from
   the embedded per-task components.

No re-run of the suite is needed. No HMAC key is needed. No access to the
operator's machine is needed.

### Python API (emit + verify)

```python
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from bernstein.eval.trajectory_receipt import build_trajectory_receipt
from bernstein.eval.trajectory_receipt_projection import (
    project_trajectory_receipt,
    verify_trajectory_receipt_projection,
    verify_cose_projection_bytes,
)

# Operator: emit and project
sk = Ed25519PrivateKey.generate()   # or load from key store
receipt = build_trajectory_receipt(...)
projection = project_trajectory_receipt(receipt, signing_key=sk)

# projection.cose_bytes    — send to external reviewers
# projection.intoto_dict   — attach to SLSA provenance
# projection.transparency_dict — log to transparency service

# Reviewer: verify with only the public key
receipt_hash = verify_cose_projection_bytes(
    projection.cose_bytes,
    public_key=sk.public_key(),   # from published JWK / key server
)
# receipt_hash == receipt.receipt_hash → fetch receipt, run verify
```

The projection is deterministic: same receipt + same signing key produces
byte-identical COSE bytes across independent calls (canonical CBOR +
deterministic Ed25519).

---

## Strip-the-substrate contract

Removing the anchored trajectory from a receipt does not produce a "warning" —
it produces a hard failure.

If `journal_head_hash` or `events_content_hash` is cleared from a task anchor
and the receipt is re-sealed, the receipt's new canonical bytes no longer match
any entry in the `eval-bench` spine. Step 7 of `verify_trajectory_receipt`
rejects it:

```
✗ receipt is not anchored in the eval-bench spine
```

The score has no meaning without the trajectory that proves it. There is no
"unverified but plausible" state.

---

## What the receipt does *not* prove

The receipt proves the score is **self-consistent with the trajectory embedded
in the receipt**. It does not prove:

- That replaying the anchored journals against the anchored fixtures on a
  different machine produces the same agent output. Live re-execution is subject
  to model drift, API changes, and timing.
- That the golden task set is representative of the capability being claimed.
  Suite design is a separate concern.
- That the operator's Ed25519 key is trustworthy. Key trust is established
  out-of-band (e.g. published in the operator's security policy).

---

## Related

- `docs/eval/bench.md` — `bernstein-bench` submission bundles and the
  leaderboard
- `docs/eval/reliability.md` — pass^k reliability floor receipts
- Issue [#2925](https://github.com/sipyourdrink-ltd/bernstein/issues/2925) —
  full design and acceptance criteria
- Issue [#2520](https://github.com/sipyourdrink-ltd/bernstein/issues/2520) —
  statistical eval-gate verdict receipts (`gate_receipt.py`)
