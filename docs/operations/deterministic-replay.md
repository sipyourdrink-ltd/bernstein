# Deterministic LLM replay (hermetic by default)

Bernstein can record every LLM call of a run to
`.sdd/runs/<run_id>/llm_calls.jsonl` and replay those responses on a later
run, so a re-run produces an identical task decomposition without paying the
model bill. This page covers the orchestrator-wired `DeterministicStore`
path (selected with `BERNSTEIN_REPLAY_RUN_ID`).

## TL;DR

| Item | Behaviour |
|---|---|
| Replay default | Strict / hermetic. A cache miss aborts the run. |
| On a miss (strict) | Raises `ReplayMissError`; the live model is never called. |
| Escape hatch | `BERNSTEIN_REPLAY_ALLOW_LIVE_MISS=1` -> miss logs a WARNING and falls through to the live model. |
| Replay key | `(model, prompt, provider, temperature, max_tokens)`. Any drift is a miss, not a hit. |
| Repeated calls | A key called N times records N responses and replays them **in recorded order**; the Nth call returns the Nth response. |
| Over-consumption | Requesting a key more times than recorded is a miss (strict: raises; non-strict: returns `None`). |
| Coverage line | `hits` / `misses` / `strict_violations`; a fully covered replay reports `misses=0`. |

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

The journal **head hash is the run identity**. Because the timing envelope is
excluded, two byte-identical executions chain to the **same** head even though
their timestamps differ, so a recording and a faithful replay match, and any
divergence (a different decision output, a reordered event, a changed event
type) changes the head at the exact step it happened.

- `bernstein replay <run-id> --verify` recomputes the chain and reports
  byte-identity, or the first divergent step index, writing a
  `divergence_report.json` (`step_index`, `expected_hash`, `actual_hash`) on
  divergence.
- `bernstein replay <run-id> --from-step N` rebuilds a deterministic state
  projection over events `[0, N)`; two invocations are byte-identical.

At run finalization the journal head is sealed into the run's lineage spine
(`core/lineage/spine.py`), so the replay identity and artifact provenance share
one root.

> Note: `bernstein verify --determinism` uses a separate fingerprint over the
> WAL decision stream (`ExecutionFingerprint` in
> `src/bernstein/core/persistence/wal.py`), which already excludes the WAL
> entry timestamp. This section covers the `journal.jsonl` chain, the one
> surfaced in run metadata and the `bernstein replay` header.

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
