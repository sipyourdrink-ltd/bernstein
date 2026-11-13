# Per-profile cost attribution

Once [response-style profiles](response-style-profiles.md) are in use, two
surfaces answer "what did each profile actually cost?":

- `bernstein cost --by profile` - a live breakdown grouped by profile.
- `bernstein cost profile-report` - a content-addressed, audit-chain
  anchored report whose every figure recomputes from the ledger.

Every figure is derived strictly per ledger entry. Numbers that cannot be
computed from recorded ledger rows are omitted, never estimated.

Source:

- `src/bernstein/core/cost/profile_attribution.py` - attribution logic
- `src/bernstein/core/cost/profile_report.py` - report builder
- `src/bernstein/cli/commands/cost.py` - `cost` group and `profile-report`

## `bernstein cost --by profile`

Groups spend by the `response_profile` tag stamped onto each ledger entry
by the spawn path.

```bash
bernstein cost --by profile
bernstein cost --by profile --last 7d
bernstein cost --by profile --json
```

| Flag | Default | Meaning |
|------|---------|---------|
| `--by profile` | (grouped by model) | Group breakdown by response-style profile. |
| `--last {1h,24h,7d,30d}` | all time | Time window over the ledger. |
| `--ledger PATH` | `.sdd/cost/ledger.jsonl` | Spend ledger to read. |
| `--json` | off | Emit raw JSON. |

When the ledger path exists, attribution uses only its entries in the selected
window, with transition exclusion described below. Task records are not a
fallback in this case: an existing empty ledger, or a window containing no
ledger entries, produces an empty grouped result rather than a task-record
breakdown. If the ledger file is absent, the command instead groups task
records by their `cost_tags.response_profile`; records without that tag go to
`unattributed`. The time-window filter applies before this fallback too, so an
empty selected task-record window has no profile buckets (and if the whole cost
view has no data, the command uses its normal no-data output).

## Attribution buckets

A named profile bucket (for example, `terse`) means ledger entries carry that
`response_profile` tag and their task has no recorded profile transition.
`unattributed` means an entry has no profile tag; it does not name a profile
or imply that spend was assigned to a default profile. `excluded (profile
transition)` means the task has a recorded transition between profiles. All
ledger entries for that task go in this one bucket, not under either named
profile, because their spend cannot be reliably split between the profiles.
The excluded bucket appears only when it has entries.

For example, with three synthetic ledger records: `t-1` is tagged `terse`
with $0.10 of cost; `t-2` has no profile tag and $0.02; and `t-3` is tagged
`verbose` with $0.30 but has a recorded transition from `verbose` to `terse`.
The grouped buckets are `terse` ($0.10), `unattributed` ($0.02), and
`excluded (profile transition)` ($0.30); none of `t-3`'s cost is attributed to
either named profile.

## Transition exclusion

A task started under profile A and re-spawned under profile B must not be
credited to either profile. The spawn path records a profile-transition
event when it overwrites a previously stamped profile, and every ledger
entry of a transitioned task lands in a single **excluded** bucket.
Attribution is all-or-nothing per task; spend is never split
heuristically across profiles.

Transitions are read from `profile_transitions.jsonl` next to the ledger.

## The honesty rule

No cross-profile savings claim is produced unless **both** profiles have
at least `MIN_COMPARABLE_TASKS` (5) tasks sharing the same role and model.
Below that bar the comparison list is empty and the output states
`insufficient comparable runs` instead of a savings figure. This applies
to `bernstein cost` (the per-profile comparison section prints only when
two or more profiles appear in the window) and to the profile report.

## `bernstein cost profile-report`

Emits a content-addressed per-profile cost report. The report is computed
from recorded ledger entries only, hashed over canonical JSON with no
timestamps in the payload, written as `<sha256>.json`, and appended to the
audit chain. A third party holding the ledger can recompute it
byte-identically.

```bash
bernstein cost profile-report
bernstein cost profile-report --last 7d
bernstein cost profile-report --json
```

| Flag | Default | Meaning |
|------|---------|---------|
| `--last {1h,24h,7d,30d}` | whole ledger | Ledger window. |
| `--metrics-dir DIR` | `.sdd/metrics` | Metrics dir for the quality-outcome join. |
| `--ledger PATH` | `.sdd/cost/ledger.jsonl` | Spend ledger to compute from. |
| `--transitions PATH` | `.sdd/cost/profile_transitions.jsonl` | Profile-transition events. |
| `--audit-dir DIR` | `.sdd/audit` | Audit chain the report event is appended to. |
| `--reports-dir DIR` | `.sdd/reports/cost_profiles` | Where the `<sha256>.json` artifact is written. |
| `--eval-ab-dir DIR` | `.sdd/reports/eval_ab` | Directory of `eval ab` artifacts for evidence links. |
| `--json` | off | Emit raw JSON. |

The report carries, per profile: task count, output tokens, cost USD,
mean output tokens per task, and (joined from metrics) the verification
pass rate. Comparable cohorts that clear the honesty rule are listed
separately. Unlike `cost --by profile`, `profile-report` remains ledger-only;
it does not fall back to task records when the ledger is missing or empty.

### Audit anchoring is mandatory

The report is only trustworthy once anchored. If the audit-chain append
fails, the command exits non-zero rather than pretending the report is
valid. The chain event type is `cost.profile_report` and it embeds the
ledger line-hash range (first line, last line, count, digest) the report
was computed from.

### Evidence links

Cross-profile comparison rows link the newest `eval ab` comparison
artifact for that pair (by artifact name and SHA-256) when one exists.
These links are presentation metadata only; they live outside the report's
hashed payload so byte-identical recomputability is untouched.

## Related

- [Response-style profiles](response-style-profiles.md)
- [Profile A/B harness](profile-ab-harness.md)
- [Cost optimization](cost-optimization.md)
