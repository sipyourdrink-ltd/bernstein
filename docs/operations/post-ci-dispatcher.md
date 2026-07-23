# Post-CI dispatcher

## TL;DR

| Item | Value |
|------|-------|
| File | `.github/workflows/post-ci-dispatcher.yml` |
| Trigger | `workflow_run: CI completed` on `main` |
| Children | `auto-release`, `auto-heal`, `bernstein-ci-fix`, `bisect-on-red` |
| Boots paid | 1 per CI completion (was 5) |

Single `workflow_run: CI completed` listener that resolves the upstream
metadata once and routes to the reusable child workflows via
`workflow_call`. Replaces the sibling listeners that each paid an
independent runner cold start plus a GHA token mint per CI completion.

## What changed

The five post-CI workflows kept their file paths so existing branch
protection rules, runtime tooling, and operator runbooks continue to
resolve them by name. Their internals now declare `on: workflow_call:`
instead of `on: workflow_run:`; the dispatcher owns the upstream event.

| Workflow file | Trigger before | Trigger now |
|---|---|---|
| `auto-release.yml` | `workflow_run: CI completed` | `workflow_call` |
| `auto-heal.yml` | `workflow_run: CI completed` | `workflow_call` |
| `bernstein-ci-fix.yml` | `workflow_run: CI completed` | `workflow_call` |
| `bisect-on-red.yml` | `workflow_run: CI completed` | `workflow_call` |
| `post-ci-dispatcher.yml` | n/a (new) | `workflow_run: CI completed` |

## Sequence

```text
CI run completes on main
        |
        v
+---------------------------+
| post-ci-dispatcher.yml    |  (1 boot, reads workflow_run metadata)
|   meta:                   |
|     head_sha, conclusion, |
|     head_branch, run_id,  |
|     display_title, ...    |
+-------------+-------------+
              |
   +----------+----------+----------+-----------+
   |          |          |          |           |
   v          v          v          v           v
auto-      auto-heal  bernstein-  bisect-     (dispatcher
release               ci-fix     on-red       outputs)
(main      (failure,  (failure,  (failure,
 branch)    canonical  canonical  main)
            repo)      repo,
                       and auto-heal
                       did NOT open
                       a PR)
```

## Race resolution

Before: `auto-heal` and `bernstein-ci-fix` both listened to
`workflow_run: CI completed` and ran in parallel. On a real failure both
tried to open a heal PR on the same SHA, occasionally producing competing
patches.

After: the dispatcher serialises them via `needs:`. `bernstein-ci-fix`
runs only when `auto-heal` either skipped or did not open a PR. The
serialisation point is the reusable workflow output
`auto-heal.heal_outcome` (one of `applied`, `skipped_no_jobs`,
`failed_validation`).

## Inputs forwarded by the dispatcher

The dispatcher resolves the upstream `workflow_run` event into typed
inputs once and passes them to each child:

| Input | Used by |
|---|---|
| `conclusion` | auto-release |
| `head_branch` | auto-release, bernstein-ci-fix |
| `head_sha` | every child |
| `run_id` | every child |
| `html_url` | auto-release, bisect-on-red |
| `display_title` | auto-heal, bernstein-ci-fix (recursion guards) |
| `actor_login` | bernstein-ci-fix (bot filtering) |

## Security model

The `workflow_run` trigger is intentional and zizmor-annotated. The
dispatcher only reads metadata fields into `if:` conditions and forwards
them as typed inputs. It never executes attacker-controlled values in a
`run:` script.

Each child reusable workflow re-checks its own preconditions
(canonical-repo gate, recursion guards, bot allowlist) for
defence-in-depth. The dispatcher gates are routing decisions, not
security boundaries.

### Secret passthrough

The dispatcher does not use `secrets: inherit` (zizmor flags that as
`secrets-inherit`: every repository secret would be exposed to every
child). Each child reusable workflow declares the exact secrets it
needs in its `on: workflow_call.secrets:` block, and the dispatcher
forwards only those:

| Child | Secrets forwarded |
|---|---|
| `auto-release` | none |
| `auto-heal` | none |
| `bernstein-ci-fix` | `GEMINI_API_KEY` (optional) |
| `bisect-on-red` | none |

`GITHUB_TOKEN` is provided automatically to every reusable workflow
invocation and does not need to be listed.

## Operator runbook

| Question | Answer |
|---|---|
| Which workflow do I look at for a failed heal on a given SHA? | The dispatcher run lists each reusable child as a sub-run. Open `post-ci-dispatcher.yml` for the SHA, drill into the failing child. |
| Where does `gh run list --workflow "auto-release.yml"` go? | Still works: reusable workflow runs are listed under their workflow file even when triggered via `workflow_call`. |
| How do I disable a single child? | Set its `if:` guard to `false` in the dispatcher (preferred), or open the reusable workflow and gate its first job. |
| How do I rerun only the post-CI fanout after CI succeeded? | Rerun the dispatcher workflow; CI itself is unaffected. |

## Testing the dispatcher

A real `workflow_run` event can be observed by pushing a commit to main
and watching the dispatcher boot, or simulated locally by invoking each
reusable workflow with `gh workflow run` and synthetic inputs. The unit
test `tests/unit/test_post_ci_dispatcher_yaml.py` asserts the
dispatcher's structural invariants (trigger, children, race serialisation).
