# Deterministic LLM replay (hermetic by default)

Bernstein can record every LLM call of a run to
`.sdd/runs/<run_id>/llm_calls.jsonl` and replay those responses on a later
run, so a re-run produces an identical task decomposition without paying the
model bill. This page covers the orchestrator-wired `DeterministicStore`
path (selected with `BERNSTEIN_REPLAY_RUN_ID`).

## TL;DR

| Item | Behaviour |
|---|---|
| Coverage | The internal `call_llm` path only (manager reviews, planning, voting, janitor). CLI-adapter subprocess LLM traffic is **not** recorded. |
| Replay default | Strict / hermetic. A cache miss aborts the run. |
| On a miss (strict) | Raises `ReplayMissError`; the live model is never called. |
| No recording (strict) | Activating replay against a run with no `llm_calls.jsonl` raises `ReplayRecordingMissingError` and aborts **before any agent is spawned**. |
| Escape hatch | `BERNSTEIN_REPLAY_ALLOW_LIVE_MISS=1` -> miss logs a WARNING and falls through to the live model. |
| Replay key | `(model, prompt, provider, temperature, max_tokens)`. Any drift is a miss, not a hit. |
| Repeated calls | A key called N times records N responses and replays them **in recorded order**; the Nth call returns the Nth response. |
| Over-consumption | Requesting a key more times than recorded is a miss (strict: raises; non-strict: returns `None`). |
| Coverage line | `hits` / `misses` / `strict_violations`; a fully covered replay reports `misses=0`. |

## What replay covers (and what it does not)

Recording and replay wrap the internal native LLM client `call_llm`, which
serves Bernstein's own features (manager reviews, planning, voting, janitor).
That client is gated by `internal_llm_provider`, whose default is `none`.

The coding work itself is done by CLI agent subprocesses (qwen, claude, ...)
that talk to their provider directly and never pass through `call_llm`, so
their LLM traffic is **not** recorded on any path. On the default
`internal_llm_provider: none` + CLI-agent setup, a recording run therefore
writes no `llm_calls.jsonl`.

A path that cannot honour "cannot reach the network" must not advertise it. So
a **strict** replay whose target run has no recording (`cached_count == 0`) now
raises `ReplayRecordingMissingError` and exits non-zero at activation, before
any agent is spawned or any provider is contacted - rather than silently
running live. To run such a path anyway with
live fall-through (non-hermetic), set `BERNSTEIN_REPLAY_ALLOW_LIVE_MISS=1`. To
get a real recording, configure an internal LLM provider and record with
`BERNSTEIN_DETERMINISTIC_SEED` set.

## How to record and replay

```bash
# Record: run with a deterministic seed; LLM calls are saved to
# .sdd/runs/<run_id>/llm_calls.jsonl
BERNSTEIN_DETERMINISTIC_SEED=42 bernstein run plan.yaml

# Replay: point at the recorded run. Replay is hermetic by default.
BERNSTEIN_REPLAY_RUN_ID=<run_id> bernstein run plan.yaml
```

A replay that matches every recorded call completes with zero live provider
calls and a coverage line whose `misses=0`.

## Strict mode (the default)

Replay is hermetic: if a prompt is not in the recording (a new prompt, a
reordered tool result, a changed model id, or a drifted
provider/temperature/max_tokens), `get_replay` raises `ReplayMissError`
instead of silently calling the live model. The run aborts. This guarantees a
run launched for replay is genuinely a replay - it cannot reach the network.

`ReplayMissError` subclasses `RuntimeError` and carries the prompt `key` and
`model`, and its message names exactly how to re-record.

## The replay key folds in every response-determining input

The lookup key is a SHA-256 over
`model \x00 prompt \x00 provider \x00 repr(temperature) \x00 max_tokens`. A
cache "hit" therefore cannot mask a parameter drift: the same `(model,
prompt)` recorded at `temperature=0.7` is a **miss** when replayed at
`temperature=0.0`.

### Re-recording note (behaviour change)

Folding provider/temperature/max_tokens into the key **invalidates
`llm_calls.jsonl` files recorded before this change** - older recordings used
a narrower `model \x00 prompt` key and now read as full misses under strict
replay. To re-record, run the original workload again with
`BERNSTEIN_DETERMINISTIC_SEED` set (and `BERNSTEIN_REPLAY_RUN_ID` unset). The
`ReplayMissError` message states this inline so an operator who hits a stale
recording knows the fix without leaving the log.

## Repeated prompts replay in recorded order

`llm_calls.jsonl` is append-only: each LLM call writes one line, in call
order. When the same `(model, prompt, provider, temperature, max_tokens)` key
is called more than once in a run - a retried decomposition, a re-asked
routing question, an agent that re-issues the same probe - the recording holds
one response per call. Replay keeps a per-key FIFO and consumes the next
recorded response on each `get_replay`, so the first call replays the first
recorded response, the second call the second, and so on. A run that records
responses `A` then `B` for one key replays `A` then `B`, not `B` twice.

Requesting a key more times than it was recorded is a replay-fidelity failure
(the replay diverged from the recording). In strict mode it raises
`ReplayMissError`; in the non-hermetic escape hatch it returns `None` and falls
through. This makes a divergent replay fail loudly instead of silently
re-serving a stale response.

## Escape hatch (opt-in, non-hermetic)

For record-extend workflows you can keep the old fall-through behaviour:

```bash
BERNSTEIN_REPLAY_ALLOW_LIVE_MISS=1 BERNSTEIN_REPLAY_RUN_ID=<run_id> bernstein run plan.yaml
```

In this mode a miss emits a WARNING for each occurrence and then calls the
live provider. It is opt-in precisely so the hermetic guarantee stays closed
unless deliberately disabled; do not set it globally in CI or air-gapped
contexts.

## Canonical event journal (determinism proof across runs)

Every run records into one always-on Merkle-chained event journal at
`.sdd/runs/<run_id>/journal.jsonl` (`core/replay/journal.py`). There is no
on/off gate; `BERNSTEIN_REPLAY_RETENTION=N` bounds how many past run journals
survive on disk (oldest run directories are pruned). Each event chains as
`event_hash = H(prev_hash, event_type, payload_hash, monotonic_index)` where
`payload_hash` covers `event` plus the decision-relevant payload with keys
sorted and fixed separators, and **excludes the wall-clock envelope** (`ts`,
`elapsed_s`). The timing fields stay on the row for the operator timeline; they
are skipped only in the hash.

The journal **head hash identifies the surviving journal state**. Because the
timing envelope is excluded, two byte-identical executions chain to the
**same** head even though their timestamps differ, so a recording and a
faithful replay match. Any divergence (a different decision output, a reordered
event, a changed event type) changes the head at the exact step it happened. An
independent seal is still required to prove that this state is the complete
finished journal.

Journal verification reports three independent facts; do not collapse them:

- **chain consistency** -- the parsed rows form a valid Merkle chain. A clean
  prefix can satisfy this after complete trailing rows were removed.
- **reader coverage** -- every non-blank physical line entered verification.
  Tolerant reads still skip torn or malformed rows, but return their physical
  line indices instead of hiding the loss. Blank lines are neither rows nor
  discards.
- **journal identity** -- the head and event count match an independent seal.
  With no seal the verdict is `unverifiable`, never a pass. Clean one- or
  multi-row truncation is intentionally undetectable without that external
  commitment.

`load_events()` returns a `JournalLoadResult`; ordinary callers consume its
`events`, while completeness-sensitive callers must also inspect
`discarded_line_indices`. `verify_journal()` returns a
`JournalVerifyResult` with `chain_consistent`, `coverage`, and `identity`
instead of the ambiguous former `ok` flag.

- `bernstein replay <run-id> --verify` recomputes the chain, reports reader
  coverage and whether journal identity is externally verifiable, or the first
  divergent step index, writing a
  `divergence_report.json` (`step_index`, `expected_hash`, `actual_hash`) on
  divergence.
- `bernstein replay <run-id> --from-step N` rebuilds a deterministic state
  projection over events `[0, N)`; two invocations are byte-identical.

Capsule-governed finalization can seal the journal head and event count outside
the journal. Where that seal exists, finished-journal identity and artifact
provenance can share an independently committed root; most task journals do
not yet carry such a seal and therefore remain `unverifiable` as complete
journals.

> Note: `bernstein verify --determinism` uses a separate fingerprint over the
> WAL decision stream (`ExecutionFingerprint` in
> `src/bernstein/core/persistence/wal.py`), which already excludes the WAL
> entry timestamp. This section covers the `journal.jsonl` chain, the one
> surfaced in run metadata and the `bernstein replay` header.

## Signed run receipt (one file, offline verification)

The journal, the lineage spine, and the audit chain each verify their own
slice, and two of the three need the operator's key material or a live
`.sdd/`. The **run receipt** (`core/replay/run_receipt.py`, issue #2924) binds
them into one signed artefact: the file alone proves integrity, the file plus
the operator's out-of-band public key proves provenance (see the trust-model
note below):

```bash
# Build: signs .sdd/runs/<run_id>/run-receipt.json
bernstein verify run <run_id> --signing-key-path /path/to/ed25519.pem

# Verify: reads ONLY the receipt file - no HMAC key, no .sdd/
bernstein verify receipt /path/to/run-receipt.json [--public-key trusted.pub.pem]
```

The receipt embeds:

- **`journal`** - the timing-excluded projection of every embedded journal row (the
  exact bytes `compute_event_hash` covers; `ts`/`elapsed_s` never enter the
  receipt) plus the head hash.
- **`spine`** - every spine entry body **without** the keyed `hmac` tag. Spine
  `entry_hash` values are plain SHA-256 over caller-visible fields, so the
  whole chain recomputes without the operator's HMAC key.
- **`audit_range`** (opt-in via `--include-audit-range --audit-since
  --audit-until`) - a re-chained audit slice whose `head_sha256` recomputes
  from the embedded events. The operator HMAC key is needed at *build* time to
  re-chain the slice, never at verify time.
- **`signing`** - a detached Ed25519 signature (RFC 8032, deterministic) over
  the DSSE pre-authentication encoding of the canonical subject binding - the
  block carrying the run id and every head - plus the public key embedded as
  an RFC 7517 / RFC 8037 OKP JWK.

The signed subject is *derived from* the embedded ranges: the verifier
recomputes every head from the receipt bytes, rebuilds the binding, and only
then checks the signature. Mutate one embedded row and verification names the
exact divergent step; strip a range and the signed subject becomes
unreachable - the receipt stops verifying, it does not merely lose a line.
Receipt bytes are byte-identical across independent builds of the same run.
The build side is strict too: an unparseable journal or spine row refuses the
whole build with the physical line named - a receipt is never signed over the
parseable subset of a corrupted store.

Exit codes for `bernstein verify receipt`:

| Exit code | Tier / condition |
|---|---|
| `0` | OK -- either tier (integrity-only or provenance) passed, and `--require-provenance` was not given, or was given and provenance was reached. |
| `1` | Empty or malformed input (unreadable file, missing ranges/fields). |
| `2` | Tamper detected (first divergent journal step index named), or a `--public-key` pin that does not match the embedded key. |
| `3` | `--require-provenance` was given and only the integrity-only tier was reached. |

**Trust model - what a pass proves depends on where the key came from.** By
default the verifier checks the signature against the Ed25519 key embedded in
the receipt (trust-on-first-use). That makes a pass **integrity-only**: the
file is internally consistent - every head recomputes from the embedded
ranges and any post-signing mutation is caught and localized to a precise
step. But an attacker who controls the whole file could have swapped in
their own key and re-signed it, so the embedded key cannot establish *who*
produced the receipt. For **provenance** - confirming the receipt was signed
by a specific operator's key - pass `--public-key` with an out-of-band copy
of that key; the embedded key must then match it or verification fails with
exit `2`. The CLI verdict is labelled accordingly (`OK (integrity-only:
embedded key)` vs `OK (provenance: pinned key)`), so a trust-on-first-use
pass cannot be misread as an authenticated one. Provenance-sensitive review
(incident triage, compliance handover) should always pin.

**Gating automation on the tier alone.** Exit `0` is indistinguishable
between the two tiers by itself, so a script that must accept only a
provenance pass should not rely on `0` on its own. Two ways to do that
without parsing the verdict prose:

- Pass `--require-provenance`: without a pinned, matching key the command
  exits `3` instead of `0`, naming the tier it actually reached.
- Pass `--json`: the response carries a `"tier"` field
  (`"provenance"`, `"integrity-only"`, or `null` when verification did not
  pass at all) alongside `"ok"`. `--require-provenance` and `--json` compose:
  the JSON body still reports `"tier": "integrity-only"` on an exit-`3`
  refusal, so a caller can log which tier was reached even when gating on
  the exit code.

Both flags default off, so a `verify receipt $f && deploy` script written
against today's behaviour keeps exiting `0` on either tier unless it opts
in.

**Automatic receipts at finalization.** When a signing key is configured via
`BERNSTEIN_RUN_RECEIPT_SIGNING_KEY_PATH` (key file) or
`BERNSTEIN_RUN_RECEIPT_SIGNING_ENV_VAR` (name of an env var carrying a PEM
key), the orchestrator writes `run-receipt.json` next to `journal.jsonl` when
the run finalizes. `BERNSTEIN_RUN_RECEIPT_SIGNING_KID` optionally sets the JWK
`kid`. With no key configured this is a documented no-op - a receipt is
only ever emitted signed, matching the audit-receipt posture.

The legacy `bernstein verify` flag modes (`--wal-integrity`, `--determinism`,
`--memory-audit`, `--formal`, and the positional wheelhouse path) are
unchanged: they route to the default `legacy` subcommand of the promoted
group with identical behaviour and exit codes. One routing edge: a wheelhouse
directory literally named `run`, `receipt`, or `legacy` shadows the
positional mode - spell it `./run` or use `bernstein verify legacy <path>`.

## Provider-side context mutations

Deterministic replay assumes context-as-sent equals context-as-consumed. The
client half is defended by the compaction policy recorded in the step
fingerprint (`apply_compaction_policy` in `src/bernstein/adapters/claude.py`),
but providers also mutate model context server-side: compaction and similar
opaque state carried between calls. Every observable mutation signal is
therefore chained into `journal.jsonl` as a first-class entry
(`src/bernstein/core/replay/provider_state.py`).

| Item | Behaviour |
|---|---|
| Journal entry | `provider_state_mutation`, content-addressed over `(kind, before_digest, after_digest, step_index)` |
| Load-bearing | The entry participates in the Merkle chain: removing or editing it breaks `--verify` at exactly its index |
| Deterministic modes | Suppression is requested at spawn (`DISABLE_AUTO_COMPACT`); a mutation that still arrives is recorded **flagged** and `bernstein replay <run-id> --verify` exits non-zero (fail-closed) |
| Live runs | Mutations are permitted but pinned: each is recorded before execution continues, so the journal head commits to it |
| Capture failure | If capture itself fails (sidecar I/O or parse error) in a deterministic/replay run, a `provider_state_capture_failed` marker is chained so `--verify` fails closed instead of reading the dropped mutation as an absence |
| Divergence attribution | `bernstein replay diff A B` reports reason code `provider_state_mutation` with the mutation kind and the exact step index when the first mismatching event is a mutation entry |
| Capability record | One `provider_state_capability` entry per resolved adapter per run: `observed` or `declared-blind` |
| Audit mirror | Each chained mutation is mirrored into the HMAC audit chain as a `provider.state_mutation` event anchored to the journal head |

The capability record is what keeps an empty run interpretable: an adapter
that cannot observe mutations (`declared-blind`) produces no mutation entries,
and the journal says so explicitly, so an absence of entries is
distinguishable from an inability to see them. The claude adapter observes
mutation signals (`compact_boundary` and related stream-json `system`
subtypes) through its wrapper, which appends each one to
`.sdd/runtime/provider_state/<session_id>.jsonl` in observation order; the
sidecar is reset at spawn so a reused session id (deterministic replay pins
them) cannot re-journal a prior run's mutations. The orchestrator chains them
into the run journal when the agent is reaped.

The digests are content addresses of the provider-reported metadata, which is
the only observable surface for a server-side rewrite: `before_digest` covers
the fields the provider labelled as pre-mutation state (`pre_*` / `before_*`),
`after_digest` covers the full reported payload. A journal head therefore
certifies either what the model consumed or the precise point where
visibility ends.

## Live thread stream

The TUI and web UI render the run as a live SSE stream that is a hash-anchored
projection of the same `journal.jsonl` chain, rather than a timer poll of the
server. Each streamed event carries its journal entry's `event_hash`, so the
operator's view is an attestable projection of what executed.

- `bernstein thread verify --run <id>` proves the projection equals the
  executed journal: it recomputes the Merkle chain
  (`verify_thread_against_journal` in
  `src/bernstein/core/replay/thread_projection.py`) and confirms every
  projected event carries the byte-identical entry hash. Exit 1 on divergence
  (reporting the first divergent step index), exit 2 when the run journal is
  missing.
- The projection is a pure function of the journal
  (`project_journal(path, after_index=...)`), so a dropped-and-reconnected
  client resumes from `Last-Event-ID` (the monotonic journal index) without
  missing or duplicating a row.
- Set `BERNSTEIN_TUI_STREAM=1` to drive the TUI hot path from the stream; unset
  keeps the polling fallback for constrained terminals. The rendering is
  unchanged - only the data source is swapped.
- An approval resolved over the stream is itself a signed record: it is
  appended to the HMAC audit chain as a `thread.approval` event
  (`record_thread_approval` in `src/bernstein/core/security/audit_chain.py`)
  anchored to the exact journal index and entry hash the operator saw.

## Related

- Source: `src/bernstein/core/orchestration/deterministic.py`
- Call site: `src/bernstein/core/routing/llm.py` (`call_llm`)
- Canonical event journal: `src/bernstein/core/replay/journal.py`
  (`EventJournal`, `verify_journal`, `rebuild_state`, `seal_journal_into_spine`)
- Replay-log readers: `src/bernstein/core/persistence/recorder.py`
  (`compute_replay_fingerprint`, `load_replay_events`)
- Sibling subsystem with the same miss contract:
  `src/bernstein/core/replay/gateway.py` (`ReplayMissError`); its replay
  fixtures consume in recorded `seq` order, so duplicate response values cannot
  desync the by-kind FIFO fallback.
