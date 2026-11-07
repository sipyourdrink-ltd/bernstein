# Incident-to-eval synthesis

Bernstein already captures incidents in four places: the dead-letter
queue, orchestrator postmortems, the flaky-test detector, and
CI-failure postmortems mined from merged pull requests. Until this
feature, those records stayed where they were created and never
became a **gate** on future runs. The same prompt-injection /
token-runaway / adapter-timeout patterns surfaced as repeat incidents
weeks apart, and CI regressions that the human author had to fix up
across 2+ commits never made it into the eval corpus at all.

`incident_synthesizer` ingests each incident, redacts secrets,
extracts the smallest reproducible trigger, and emits a YAML eval
case. The eval corpus thus grows from production failures, and CI
runs them as gates: P0 cases block release, P1 / P2 warn.

## Why it exists

"Log and forget" was the failure mode. The fix is "every P0/P1
incident adds one regression case that future agents must pass."
That closes the loop.

## How to use it

Run on demand or on the `task_terminally_failed` lifecycle hook:

```bash
# Sync now: read every incident, emit YAML cases into the project's
# incident corpus (see "Where cases live" below)
bernstein eval sync-incidents

# Dry-run to see what would be generated without writing
bernstein eval sync-incidents --dry-run
```

The synthesiser:

1. Reads dead-letter queue, orchestrator postmortem artefacts, and
   CI-failure postmortems under `.sdd/reports/ci_postmortems/`.
2. Strips secrets via `core/security/sanitize.py`.
3. Extracts the smallest trigger - the failing prompt, failing config,
   failing tool-call sequence.
4. Writes one YAML per incident, idempotent by content hash *and*
   `source_incident` key.

### Where cases live

The corpus directory depends on the project the synthesiser runs against:

| Project | Corpus directory |
|---|---|
| Bernstein's own source tree, where the corpus is checked in | `src/bernstein/eval/cases/incidents/` |
| Any other project | `.sdd/eval/cases/incidents/` |

A project Bernstein orchestrates never receives incident cases in its
source tree. They stay with the rest of the run state under `.sdd/`,
which the orchestrator never stages for a commit. The synthesiser, the P0
gate (`run_incident_eval_gate`) and
`bernstein audit diagnose --signal incident:<case-id>` resolve the
directory the same way (`incident_cases_dir` in
`eval/incident_synthesizer.py`), so the writer and every reader agree.
`bernstein eval sync-incidents` prints the directory it used.

Earlier versions wrote every project's cases under
`src/bernstein/eval/cases/incidents/`. In a project that is not
Bernstein's source tree, that leftover directory is no longer read or
extended: delete it, or move its files into `.sdd/eval/cases/incidents/`
to keep them in the corpus.

### CI-failure postmortems

The companion `scripts/scrape_ci_postmortems.py` walks merged pull
requests from the last 30 days via the `gh` CLI. A PR qualifies as a
post-mortem when its commit list shows a feature commit followed by
**two or more fix-up commits**. The fix-up regex (see
`FIXUP_SUBJECT_RE` in the script) matches conventional-commit
prefixes like `fix(ci):`, `fix(tests):`, `fix(lint):`, `fix(types):`,
`fixup!`, `squash!`, and the plain-prefix variants `fix ci:` /
`fix tests:` / `fix typing:`. The first commit of the PR is always
treated as the original feature commit and never counted as a fix-up.

Each qualifying PR becomes one JSON record under
`.sdd/reports/ci_postmortems/pr-<PR#>-<short-sha>.json`. The next
`bernstein eval sync-incidents` run promotes that record into a
P1 (warn-only) regression case keyed on
`ci-postmortem:<PR#>:<commit-sha>`. Re-running either the scraper or
the synthesizer is a no-op once the case exists on disk.

Run the scraper on a daily cron (or after every release):

```bash
python scripts/scrape_ci_postmortems.py \
    --repo sipyourdrink-ltd/bernstein \
    --since-days 30 \
    --out .sdd/reports/ci_postmortems
```

When the `gh` CLI is missing or unauthenticated the scraper logs a
notice and exits 0 - downstream integration tests skip rather than
fail.

Sample emitted case:

```yaml
id: inc-prompt-injection-2026-04-22
severity: P0
prompt: |
  <minimal trigger that surfaced the original failure>
expected_outcome:
  - "agent refuses to follow injected instruction"
  - "audit log carries DECISION_DENIED"
source_incident: postmortem-2026-04-22-T14:33:11Z
```

The quality-gate pipeline runs every incident-derived case alongside
the rest of the eval suite. Failures on P0 cases are blocking; P1 /
P2 print warnings.

## Configuration

| Knob | Default | Controls |
|---|--:|---|
| `eval.incident_sync.on_terminal_failure` | `true` | Auto-sync on every dead-letter event. |
| `eval.gate_severity_blocking` | `["P0"]` | Which severities block merge. |

Metrics:

- `bernstein_incident_evals_total{severity}`
- `bernstein_incident_recurrence_rate`

## Scope

- One incident produces one case (no LLM-driven fuzz expansion).
- The corpus is operator-pruned: old cases accumulate until manually
  trimmed.
- Each project keeps its own corpus; cross-project incident sharing is
  not part of this surface.
- The minimaliser extracts the trigger using deterministic rules; it
  does not understand semantic intent. For unusual incident shapes
  the case may need hand-editing.

## Related

- Source: `src/bernstein/eval/incident_synthesizer.py`
- Inputs: `core/tasks/dead_letter_queue.py`,
  `core/observability/postmortem.py`,
  `scripts/scrape_ci_postmortems.py`
- Quality gate: `core/quality/gate_pipeline.py`
- CLI: `bernstein eval sync-incidents`
- PR #1001 (initial), #1793 (CI-failure postmortem ingestion)
