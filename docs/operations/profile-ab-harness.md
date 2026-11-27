# Profile A/B harness (`bernstein eval ab`)

`bernstein eval ab` runs one eval suite under two (or three)
[response-style profiles](response-style-profiles.md) and emits a single
deterministic comparison artifact carrying **both** the cost delta and the
quality delta. It answers "does `terse` actually save tokens without
hurting pass rate?" with a numbers-you-can-recompute artifact rather than
a hand-rolled comparison.

For the lower-level offline prompt-vs-prompt primitive this builds on, see
the [A/B runner primitive](../eval/ab-runner.md).

Source:

- `src/bernstein/eval/ab_comparison.py` - three-arm comparison + artifact
- `src/bernstein/eval/ab_runner.py` - runner, executors, YAML loaders
- `src/bernstein/cli/commands/eval_benchmark_cmd.py` - `eval ab` command

## Two modes

The command has two modes, selected by which options you pass:

| Mode | Options | What it does |
|------|---------|--------------|
| Variant | `--variant-a` / `--variant-b` / `--tasks` | Compares two prompt variants with the deterministic echo executor. Offline, zero LLM cost. |
| Suite (profile arms) | `--suite` / `--arm-a` / `--arm-b` | Runs the suite under two response profiles; emits one chain-anchored cost-and-quality artifact. |

Any one of `--suite`, `--arm-a`, or `--arm-b` selects suite mode, even
when all variant inputs are also supplied. Suite mode then requires all
three suite options together; a partial suite selection is a usage error.

Variant mode prints comparison JSON by default; `--json` is not needed
and does not change that mode. With `--output FILE`, it writes that JSON
to the file and prints a short confirmation instead.

Suite mode defaults to a human-readable comparison; `--json` prints its
artifact path, digest, and content as JSON. `--output FILE` additionally
writes that suite JSON payload without changing the selected stdout
format. Suite mode also stores its content-addressed artifact under
`--reports-dir`.

## Suite mode: the three-arm plan

Comparing a profile against an unconstrained baseline conflates the
profile's own content with the generic instruction to be brief. To keep
the delta honest, `--arms 3` runs three arms:

| Arm | Profile | Purpose |
|-----|---------|---------|
| `baseline` | unset | No style addendum at all. |
| `control` | minimal built-in terse addendum | Isolates the effect of "be brief" alone. |
| `candidate` | the named profile | The profile under test. |

- The **honest delta** is candidate vs control.
- Candidate vs baseline is still reported, but labelled `conflated`.

With `--arms 2` (default) the two named profiles are compared directly
without the control arm.

## Usage

```bash
# Variant mode (offline, deterministic)
bernstein eval ab --variant-a a.yaml --variant-b b.yaml --tasks tasks.yaml

# Suite mode, two arms (offline synthetic executor)
bernstein eval ab --suite suite.yaml --arm-a balanced --arm-b terse --executor synthetic --json

# Suite mode, three arms with the built-in control, 3 trials each
bernstein eval ab --suite suite.yaml --arm-a baseline --arm-b terse \
  --arms 3 --trials 3

# Suite mode, spawn real workers instead of the synthetic executor
bernstein eval ab --suite suite.yaml --arm-a balanced --arm-b terse \
  --executor spawn
```

| Flag | Default | Meaning |
|------|---------|---------|
| `--suite FILE` | - | Suite YAML with a top-level `tasks: [...]` list. |
| `--arm-a NAME` / `--arm-b NAME` | - | Profile for each arm (`baseline`, `balanced`, `terse`, `verbose`). |
| `--arms {2,3}` | 2 | 3 adds the unset baseline and the built-in minimal-control arm. |
| `--trials N` | 1 | Trials per `(arm, task)` pair. |
| `--executor {synthetic,spawn}` | synthetic | `synthetic` is deterministic and offline; `spawn` runs each arm as a real task in an isolated worktree. |
| `--model NAME` | - | Model pin for spawned arms and the model label in the artifact. |
| `--role NAME` | backend | Agent role for spawned arms. |
| `--timeout SECONDS` | 1800 | Max wait per spawned task. |
| `--ledger PATH` | synthetic: `.sdd/eval/ab/ledger.jsonl`; spawn: `.sdd/cost/ledger.jsonl` | Spend ledger the runs are joined against. |
| `--reports-dir DIR` | `.sdd/reports/eval_ab` | Where the content-addressed artifact is written. |
| `--audit-dir DIR` | `.sdd/audit` | Audit chain the comparison event is appended to. |
| `--json` | off | Emit raw JSON in suite mode; variant mode already defaults to JSON. |
| `--output FILE` | unset | Write the selected mode's comparison JSON here; see the mode-specific stdout behavior above. |
| `--scorer {exact,none}` | exact | Built-in scorer for variant mode. |

Synthetic figures never land in the production spend ledger; they write to
a separate `.sdd/eval/ab/ledger.jsonl` by default.

## The artifact

The suite-mode artifact is canonical JSON (sorted keys, compact
separators, ASCII escapes), carries no timestamps in the hashed payload,
is content-addressed on disk, and produces exactly one audit-chain event
per emission (`eval.ab_comparison`).

- **Ledger-anchored cost.** Every cost figure is a sum over concrete
  spend-ledger rows, each referenced by the SHA-256 of its raw JSONL line
  bytes. A verifier holding the ledger resolves each reference and
  recomputes every aggregate.
- **Aggregation uses medians** (median tokens, median USD, pass rate).
- **No partial winners.** A winner is declared only when both cost and
  quality are measured for both honest arms. A run missing either emits
  `incomparable`. Unmeasured axes are declared as structured
  `not measured` fields, never prose.

Emitted artifacts are also registered in a pair index that
[`bernstein cost profile-report`](profile-cost-attribution.md) links from,
so a per-profile cost report can cite the A/B evidence for each pair.

### Audit anchoring is mandatory

As with the profile report, the artifact is only trustworthy once
anchored. If the audit-chain append fails, the command exits non-zero
rather than emitting an unanchored artifact.

## Related

- [Response-style profiles](response-style-profiles.md)
- [Per-profile cost attribution](profile-cost-attribution.md)
- [A/B runner primitive](../eval/ab-runner.md)
