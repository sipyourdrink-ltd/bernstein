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

When the ledger is present, attribution is per entry with transition
exclusion (below). When it is missing, the command degrades to the
`response_profile` tag on task records so older runs still show a
breakdown.

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
separately.

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
