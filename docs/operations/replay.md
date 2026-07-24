# Per-step session replay (issue #1799)

A per-step replay surface lets an operator walk an agent run forward
and backward, fork a divergent branch at a specific step, and emit
portable, offline-verifiable receipts of the chain.

## TL;DR

| Verb | What it does |
|---|---|
| `bernstein replay <agent_id>` | Render the hash-chained step view; verify chain integrity before display |
| `bernstein session fork <session_id> --from-step <n>` | Materialise a sibling worktree branched at parent step N; chain becomes a tree |
| `bernstein replay export <agent_id> -o RECEIPT` | Portable, content-addressed receipt; offline-verifiable with the install public key |
| `bernstein replay publish <agent_id> -o RECEIPT --opt-in` | Redacted receipt; only path that ever writes outside `.sdd/runtime/` |
| `bernstein replay verify <RECEIPT> [--head HEX]` | Offline verifier |
| `bernstein replay diff-journal <A> <B>` | Surface the precise field that differs between two chains |
| `bernstein replay debug <RUN>` | Forensic single-chain walk; refuse a tampered chain and localise the divergent step; emit an offline-verifiable debug receipt |
| `bernstein replay debug <LEFT> <RIGHT>` | Two-run time-travel path diff; content-addressed, byte-identical artifact |
| `bernstein replay debug <RUN> --fork-from <n>` | Fork-and-reproduce a worktree anchored at parent step N |

Privacy default is local-only. `publish` is opt-in.

## Two debugging jobs: exploratory vs forensic

Replay debugging answers two different questions; do not confuse them.

| Job | Question | How | Surface |
|---|---|---|---|
| **Exploratory what-if** | "What changes if I re-run this?" | Re-execute the task (optionally against recorded fixtures or a different model) and compare the *new* output | `bernstein replay <run_id>` (run-trace replay), `bernstein replay <task_id> --model ...` |
| **Forensic reconstruction** | "Where exactly did this run diverge, and can I prove it?" | Freeze the recorded chain; recompute hashes; never re-execute anything | `bernstein replay debug ...` (this section) |

`replay debug` is **forensic**. It re-executes nothing. Its deliverable is
a **debug receipt** that verifies offline via `bernstein replay verify` and
that is meaningless once the Merkle chain it anchors on is stripped or
corrupted. A non-deterministic divergence is surfaced as a hash mismatch at
the exact diverging step - not as a "flaky, re-ran it" signature - so a
flaky run becomes a reproducible bug report.

## Journal layout

```
.sdd/runtime/journal/<agent_id>/000000.jsonl
```

One JSON object per line, append-only. Each line carries the canonical
six fields plus `seq`, `step_hash`, `ts`, and a list of CAS `blob_refs`.

## Step-hash encoding

```text
step_hash = SHA256(
    canonical_json({
        "prev_hash":   <step_hash of step N-1, or "0"*64 for genesis>,
        "input_hash":  <SHA-256 hex of the user-supplied input blob>,
        "model":       <e.g. "claude-3-7-sonnet-20250219" | null>,
        "prompt":      <full prompt text the adapter received | null>,
        "tool_call":   <serialised tool invocation dict | null>,
        "tool_result": <serialised tool result dict       | null>,
    })
)
```

Canonical JSON: `json.dumps(..., sort_keys=True, separators=(",", ":"))`,
UTF-8 encoding. A peer can re-derive any step hash by hand from these
six fields without running our code.

This is a versioned contract; any change to the field set or the encoding
is a `format_version` bump in the receipt manifest.

## Verification

`JournalReader.verify(expected_head=...)` walks the chain from genesis
to the tail. Errors surface as:

- malformed JSON line
- `prev_hash` mismatch (chain break)
- `step_hash` mismatch (field tamper)
- head mismatch against the caller-supplied expectation

Each error carries the offending line number so an operator can grep
the file.

## Recovery on open (fail-closed)

`Journal.open` recovers the chain head before any new step is appended
(after a crash, a restart, or when seeding a fork). Recovery **revalidates
the hash chain** rather than trusting the last on-disk row: it walks the
bucket from genesis, recomputes every `step_hash` with the same
`compute_step_hash` primitive `verify` uses, checks `prev_hash` linkage and
`seq` continuity, and takes the tip from the last *recomputed* hash.

Recovery fails closed. If a parseable row does not verify - a recomputed
`step_hash` that differs from the stored value, a `prev_hash` that does not
chain onto the previous row, or a `seq` gap - `Journal.open` raises
`JournalError` naming the offending line, instead of adopting the row and
letting subsequent appends grow valid-looking children on a poisoned anchor.
This closes the gap where a tampered or truncated-then-edited journal could
be silently extended after a restart or a `session fork --from-step`.

Distinct from tampering: a **torn or unparseable trailing line** (a writer
killed mid-write) still degrades gracefully. Recovery stops at the last
validated row and a subsequent append chains onto it, preserving the
legitimate crash-recovery path. A malformed line that is *followed* by a
well-formed row is treated as interior corruption and raises.

Operator remedy when recovery refuses to open: the error names the bucket
file and the offending line. Move the corrupt journal aside (for example,
rename `000000.jsonl` to `000000.jsonl.corrupt`) so the agent can start a
fresh chain, and keep the quarantined file for forensic inspection with
`JournalReader.verify`.

Cost note: recovery now recomputes every step hash on open (O(steps)) rather
than reading the tail (O(1)). For very long single-agent runs this is a
measurable - but bounded - startup cost, paid once per open.

## Fork-from-step

`bernstein session fork <session_id> --from-step <n>` materialises a
sibling worktree branched from the parent's current commit, seeds the
fork's per-step journal with the parent prefix `[0..n]`, and records
`fork.from_step` + `fork.parent_step_hash` in the snapshot. Subsequent
agent activity in the fork chains on top of that prefix, so the family
of forks forms a tree rather than a flat list.

When `--from-step` is omitted the command falls back to the pre-#1799
session-level fork semantics. The two paths share a single
implementation in `bernstein.core.sessions.fork.fork_session`.

## Replay debug (forensic time-travel)

`bernstein replay debug` composes the record, journal, diff, and fork
primitives into one operator-facing debugger. It is a projection of the
chain that already exists on disk - no new hashing scheme, no new storage.
The core lives in `bernstein.core.replay.debug`.

### Single-run walk

```
bernstein replay debug <RUN> [--json] [--full] [--limit N] [--sign KEY]
```

1. Verifies the chain head via `JournalReader.verify` **before** any output.
2. If the chain does not verify, it is **refused** (non-zero exit). The
   command still localises the first divergent step by streaming
   `walk_and_verify` from `JournalReader` (it never materialises the whole
   journal), reporting `HashMismatch(seq, expected_hash, actual_hash,
   first_divergent_field)`:
   - a `prev_hash` linkage break names `prev_hash` (attributed via
     `journal_diff.diff_steps`);
   - a bare digest tamper (the row's fields no longer hash to its stored
     `step_hash`) names `step_hash`.
   Only the first divergent step is reported, so the signal is a single
   named step, not a cascade of downstream breaks.
3. On a healthy chain it writes a **debug receipt** via
   `journal_export.export_receipt` to `.sdd/runtime/receipts/<run>.debug.tar`
   and prints the head hash and a bounded step projection (`--full` lifts
   the default cap; `--limit N` sets it). `--sign KEY` signs the receipt
   with an `Ed25519FileKeySigner`.

The debug receipt IS the deliverable: `bernstein replay verify <receipt>`
accepts it offline, and stripping or corrupting the bundled chain (or its
recorded head) makes verification fail - the debugger's output is
meaningless without the audit chain, not merely unlogged.

### Two-run path diff

```
bernstein replay debug <LEFT> <RIGHT> [--json] [--jump-to-failure]
```

Reuses `journal_diff.diff_journals` to find the first divergence, then emits
an ordered side-by-side of the canonical six fields from seq 0 up to and
including the divergence, with the diverging fields marked. The artifact is
**content-addressed**: its `diff_hash` is a SHA-256 over the sorted-key JSON
of the diff body (chain content only, never a filesystem path), written to
`.sdd/runtime/debug/<left>__<right>.pathdiff.json`. Two operators on the
same journals - or the same operator twice - produce the byte-identical
artifact and the same `diff_hash`.

`--jump-to-failure` positions the output at the diverging seq; it is a
presentation flag and does not perturb the content-addressed artifact.

### Fork-and-reproduce

```
bernstein replay debug <RUN> --fork-from <n>
```

Calls `fork_session(..., from_step=n)` to materialise a sibling worktree
seeded with the parent journal prefix `[0..n]`, so the fork's first new step
chains onto the parent `step_hash` at seq `n`. The debug output records that
`parent_step_hash` as the reproduction anchor. An out-of-range `n` fails
fast with no side effects on disk (no dangling worktree or branch).

### Privacy

`replay debug` is local-only by default: the receipt and the path-diff
artifact are written under `.sdd/runtime/`. It has no publish path. Should a
receipt ever be published, it routes through the same `RedactionPolicy` as
`replay publish`.

## Receipt format

A receipt is a tarball:

```text
manifest.json        # canonical-JSON manifest header
manifest.sig         # optional base64 Ed25519 signature over manifest bytes
journal/000000.jsonl # canonical chain bytes
blobs/<digest>       # referenced CAS payloads (best-effort)
```

The manifest carries `agent_id`, `head_hash`, `steps`,
`bernstein_version`, `created_at`, `blob_digests`, and `format_version`.
`verify_receipt` walks the chain end-to-end and asserts the manifest
matches what was walked.

## Publish flow (privacy redaction)

`bernstein replay publish <agent_id> --opt-in` runs the configured
`RedactionPolicy` (default redacts `prompt` and `tool_result`),
re-anchors the chain to the redacted payloads, and writes a receipt
with the new head hash. The original local chain is untouched. The
published receipt remains offline-verifiable against its (different)
head hash; consumers must not rely on the original head when verifying
a published receipt.

## Audit-chain integration

New event types under `bernstein.core.security.audit`:

| Event type | Emitted when |
|---|---|
| `replay.step` | An entry is appended to the journal |
| `replay.fork` | A `session fork --from-step` materialises a sibling worktree |
| `replay.export` | A receipt is written via `replay export` |
| `replay.publish` | A redacted receipt is published |

These add to the existing event-type registry without modifying any
prior entries. The audit-slice extractor picks them up via the standard
`event_type=` filter.

## Provider-side context mutations

Provider-side context rewrites (compaction boundaries and similar opaque
state markers surfaced in provider responses) are chained into the run
journal as content-addressed `provider_state_mutation` entries, so the
journal head commits to every observed rewrite before anything builds on
it. In deterministic modes an arriving mutation is recorded flagged and
`bernstein replay <run-id> --verify` fails closed; `bernstein replay diff`
attributes a mutation-driven divergence with the `provider_state_mutation`
reason code, the mutation kind, and the exact step index. Each adapter's
ability to observe mutations at all is recorded per run as a
`provider_state_capability` entry (`observed` or `declared-blind`), and
each chained mutation is mirrored into the HMAC audit chain as a
`provider.state_mutation` event. Full behaviour table:
[deterministic-replay.md](deterministic-replay.md#provider-side-context-mutations).

## Backward compatibility

- `bernstein git undo <snapshot_id>` works unchanged.
- `bernstein session fork <session_id>` without `--from-step` keeps the
  pre-#1799 session-level fork semantics.
- The legacy `bernstein replay <run_id>` (run-trace replay) continues to
  work; the new per-step view is dispatched only when the journal
  directory exists at `.sdd/runtime/journal/<id>/`.
