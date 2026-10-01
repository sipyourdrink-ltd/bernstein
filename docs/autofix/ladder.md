# Autofix escalation ladder

Status: MVP. Operator-flagged off by default. Rung 0 and Rung 3 ship
with full actors. Rung 1 and Rung 2 ship with detectors only; their
actors return `stubbed` until follow-up PRs land.

The ladder scopes the smallest end-to-end self-driving CI response.
On every dispatched PR the daemon walks the rungs cheapest-first and
fires the lowest one that matches. Rungs are static in MVP: no
learned heuristics, no cross-PR memory.

## Rungs

| Rung | Detector | Action | Cost cap |
|------|----------|--------|----------|
| 0 | lint / format drift detected by `ruff check --fix --diff` | apply patch, push, comment on PR | $0 |
| 1 | single-file test failure with diff size <= 30 lines on a PR-touched file | spawn `ci-fixer` for one round, post diff for operator review (stubbed) | $0.20 |
| 2 | multi-file failure on PR-touched files | spawn `ci-fixer` + `qa` for one round each, require operator approval (stubbed) | $0.80 |
| 3 | failure on file(s) the PR did NOT touch | stop, post `out of scope - human` comment | $0 |

The daemon picks the *lowest* matching rung and refuses to escalate
above the cap declared in `bernstein.yaml: autofix.cost_cap_per_pr`.
When a rung's per-rung cap exceeds the operator cap the selector
returns `cost_capped` without firing the actor.

## Worked example: Rung 0 (lint drift)

CI surfaces:

```
src/bernstein/foo.py:1:1: E501 line too long (104 > 100)
ruff check failed: 1 error
```

The ladder fires Rung 0. Its actor runs `ruff check --fix --diff`,
commits the patch, pushes to the PR
branch, and posts a one-line comment. No model spend.

## Worked example: Rung 1 (single-file small diff)

CI surfaces a single failing test in a file the PR already modifies,
and the autofix daemon estimates the diff at 12 lines. The ladder
fires Rung 1.

In MVP the actor returns `stubbed`: the audit chain records the would-be
escalation but no agent is spawned. A follow-up PR wires the
`ci-fixer` role with one-round budget.

## Worked example: Rung 2 (multi-file failure on PR-touched files)

CI surfaces failures in `src/a.py` and `src/b.py`; the PR touches
`src/a.py`. The ladder fires Rung 2.

In MVP the actor returns `stubbed`. A follow-up PR wires the
`ci-fixer` plus `qa` pair with an operator approval gate.

## Worked example: Rung 3 (out of scope)

CI surfaces a failure in `src/legacy/auth.py`, but the PR only
touches `docs/README.md`. The ladder fires Rung 3. The actor posts a
comment explaining that the failure is outside the PR's blast radius
and labels the case for a human reviewer. No spawn, no spend.

## Audit trail

Each fire writes one lifecycle event into the autofix audit log
(`AuditLog.log("autofix.ladder.fire", ...)`) with:

* `producer = "autofix-ladder"`
* `rung_id`
* `failure_signature`
* `outcome`
* `cost_usd`
* `failing_files`, `pr_touched_files`
* `head_sha`, `run_id`

Operators can audit-replay any fire by walking the chain for
`producer=autofix-ladder` entries.

## Dry-run CLI

```
bernstein autofix ladder --dry-run --pr <number> [--repo owner/name] \
  [--log-excerpt "..."] [--failing-files a,b] [--pr-touched-files c,d] \
  [--diff-line-count 12] [--payload failure.json] \
  [--signature <sig>] [--config path/to/bernstein.yaml]
```

The CLI prints the rung that would fire, the per-rung cost cap, the
operator cap, and the acceptance reason. `--config` overrides the
`./bernstein.yaml` location. No side effects.

## Feature flag

The ladder is gated by `bernstein.yaml`:

```yaml
autofix:
  cost_cap_per_pr: 1.0
  ladder:
    enabled: false
```

Flip `autofix.ladder.enabled` to `true` after three PRs have run
clean to ramp the ladder on. The daemon refuses to act when the flag
is off, regardless of which rung matches.

## Out of scope (MVP)

The following items are scoped out of this MVP per RFC #1415 and the
backlog ticket dated 2026-05-19:

* Self-improving heuristics. Rungs are static.
* Cross-PR learning. Each PR sees the ladder fresh.
* Rung 3 escalation that acts on the codebase. v1 just posts a
  comment.
* Cost caps learned from history. Operator sets a flat USD cap.
* Rung 1 and Rung 2 actor wiring. Detector-only stubs ship now;
  follow-up PRs connect to the spawn path.
