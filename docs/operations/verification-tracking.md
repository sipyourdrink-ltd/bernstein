# Verification tracking

Bernstein flags task completions that finish without any sign of
verification - no tests, no quality gates, no completion-signal check -
and raises an alert when the rate of those unverified completions
crosses a threshold. This page is the operator's guide to that signal:
what counts as "verified", when the alert fires, how to configure it,
and what to do when one shows up.

If you just want to know:

- **Where the data lives**: `.sdd/metrics/verification_nudges.jsonl`
- **Where the alert surfaces**: `bernstein status` (CLI), `/status/`
  (HTTP), the TUI dashboard.
- **Default trigger**: more than 30% unverified, with at least 3
  completions in the window.

---

## What "verified" means

A completion is **verified** if any of these are true:

| Evidence type            | Source                       | Field                       |
|--------------------------|------------------------------|-----------------------------|
| Tests run                | Agent log summary            | `tests_run`                 |
| Quality gates run        | Quality gate result object   | `quality_gates_run`         |
| Completion signals checked | Janitor `verify_task()`    | `completion_signals_checked`|

The logic is a simple `OR`:

```
verified = tests_run OR quality_gates_run OR completion_signals_checked
```

A task whose log summary shows none of these is **unverified**. The
tracker writes one record per completion to the JSONL ledger and
stamps `task.verification_count` (0–3) and `task.flagged_unverified`
(bool) on the task object so any downstream consumer can filter on
them.

---

## When the alert fires

The tracker keeps a running summary with two parameters:

| Parameter                 | Default | What it does                                |
|---------------------------|--------:|---------------------------------------------|
| `nudge_threshold`         |    0.3  | Unverified ratio above which alerts fire    |
| `MIN_COMPLETIONS_FOR_NUDGE` |    3  | Minimum completions before threshold checks |

The math:

```
threshold_exceeded = total >= MIN_COMPLETIONS_FOR_NUDGE
                     AND unverified_ratio > nudge_threshold
```

The comparison is strict (`>`, not `>=`) - exactly 30% does **not**
trigger. The `MIN_COMPLETIONS_FOR_NUDGE` floor exists so the very
first unverified completion in a fresh session does not flip the
alert (1/1 = 100%).

### Where it surfaces

| Surface                  | Condition              | What you see |
|--------------------------|------------------------|--------------|
| `GET /status/` API       | always                 | `verification_nudge` object in JSON |
| `bernstein status` CLI   | `threshold_exceeded`   | red **ALERT** with counts and ratio |
| `bernstein status` CLI   | unverified > 0         | yellow **Notice** with counts |
| TUI dashboard            | first time threshold trips | toast notification, severity=warning, 10 s timeout |

The API response shape is small enough to paste into a runbook:

```json
{
  "total_completions": 10,
  "verified_count": 6,
  "unverified_count": 4,
  "unverified_ratio": 0.4,
  "threshold_exceeded": true,
  "nudge_threshold": 0.3,
  "recent_unverified": ["task-a", "task-b", "task-c"]
}
```

---

## Configuration

### YAML (`bernstein.yaml`)

```yaml
verification_nudge:
  threshold: 0.3            # 0.0 = alert on any unverified, 1.0 = never alert
  min_completions: 3        # how many completions before threshold matters
```

### Tightening the bar

A few tuning patterns we have seen work:

| Goal                                             | Threshold | Min completions |
|--------------------------------------------------|----------:|----------------:|
| Maximum sensitivity (CI, release branches)       |   0.10    |        3        |
| Default                                          |   0.30    |        3        |
| Sandboxes / spike work where verification is rare |   0.50    |        5        |
| "Tell me only when something is really wrong"    |   0.70    |       10        |

If you raise `threshold` above 0.5 you are silencing the signal more
than tuning it; consider whether you actually want this gate at all.

### Resetting state

The ledger is append-only. To reset between runs:

- delete `.sdd/metrics/verification_nudges.jsonl`, or
- call `tracker.reset()` from a hook or shell script.

The in-memory tracker also resets at process exit.

---

## Operator playbook

You see a red ALERT in `bernstein status`. What now?

1. **Don't panic - and don't disable the alert.** The signal is a
   ratio, not an error. It only means more than `threshold` of recent
   completions had no verification evidence at all. The agent likely
   did real work; it just did not run tests or trip a quality gate.

2. **Pull the recent unverified IDs.** From `bernstein status` or:

   ```bash
   curl -s http://127.0.0.1:8052/status/ | jq '.verification_nudge.recent_unverified'
   ```

3. **Spot-check one.** Pick a flagged task ID. Open its log summary
   and confirm: did it really skip tests, or is the agent's log
   summary missing the evidence Bernstein looks for? The latter is
   a parsing miss - fix the adapter, not the threshold.

4. **If the agent is genuinely skipping verification**, look at:
   - The plan: did the YAML omit a `verify` step?
   - The quality gates: are they wired up but failing fast?
   - The model: is it deciding "this is trivial, no test needed"
     when it actually needs one? Consider tightening prompts or
     adding a hook that forces `tests_run`.

5. **If you intentionally allow unverified completions** (e.g. doc
   fixes, single-line constants), raise `min_completions` rather
   than the threshold. That suppresses the alert during quiet
   sessions without lying about busy ones.

6. **Resolve the alert** by completing more verified tasks (the
   ratio drifts back below threshold) or by resetting the ledger
   if you need a clean baseline for a release.

The alert is **not** auto-clearing once you fix the underlying issue
- it tracks completions in a window. If the window keeps including
old unverified completions, the ratio stays high. Reset the ledger
or wait for the unverified ones to age out.

---

## Code pointers

| File                                                       | What it does |
|------------------------------------------------------------|--------------|
| `src/bernstein/core/quality/verification_nudge.py`         | `VerificationNudgeTracker`, `VerificationRecord`, `NudgeSummary`, `load_nudge_summary()` |
| `src/bernstein/core/models.py`                             | `Task.verification_count`, `Task.flagged_unverified` fields |
| `src/bernstein/core/quality/janitor.py`                    | `verify_task()` - supplies the `completion_signals_checked` evidence |
| `tests/unit/test_verification_nudge.py`                    | 44 tests across 8 classes (record, persistence, summary, alert thresholds) |

JSONL ledger schema (one object per line):

```json
{
  "task_id": "string",
  "session_id": "string",
  "timestamp": 1712200000.0,
  "tests_run": false,
  "quality_gates_run": false,
  "completion_signals_checked": false,
  "verified": false
}
```

---

## Related

- [Permission modes](../architecture/permission-modes.md) - how the
  approval gate decides whether a completion needs human signoff.
- [Runbooks](runbooks.md) - automated remediation for failing tasks.
