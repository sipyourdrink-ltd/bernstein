# Type-check scope

Which paths the type checkers actually cover, which runs can fail a pull
request, and how a path graduates from advisory to blocking.

Two of the three type runs in `ci.yml` end in `|| true`. A green PR page
therefore says nothing about the advisory runs, and an error introduced outside
a blocking scope lands without a signal. This page makes each scope explicit so
that gap is a known quantity rather than a surprise.

## TL;DR

| Run | CI job | Config | Scope | Blocking |
|---|---|---|---|---|
| mypy gated zone | `mypy strict (lineage substrate)` | `mypy.gate.ini` | `files` minus `exclude` | Yes - in `ci-gate` needs |
| mypy parse check | `mypy strict (lineage substrate)` | `[tool.mypy]` | `src/` | Only on a parse abort |
| mypy advisory | `mypy strict (lineage substrate)` | `[tool.mypy]` | `src/` | No - error count reported |
| pyright strict zone | `Pyright strict (security + cluster)` | `pyrightconfig.strict.json` | `include` list | Yes - in `ci-gate` needs |
| pyright advisory | `Type check report` | `[tool.pyright]` | repo-wide | No - job is not in `ci-gate` needs |

## Blocking scopes

### mypy - `mypy.gate.ini`

Strict mode over the lineage substrate. The zone is exactly the `files` list
minus the `exclude` regex:

| Field | Meaning |
|---|---|
| `files` | `src/bernstein/core/evidence`, `.../identity`, `.../lineage`, `.../persistence` |
| `exclude` | Modules inside those packages that are not strict-clean yet |
| `follow_imports = silent` | Imports outside the zone are resolved for types but their errors are not reported |

Widen the zone by deleting an entry from `exclude`, then by adding a package to
`files`. Both directions are a config-only change; no workflow edit is needed.

A path **not** in the zone is checked only by the advisory run below.

### pyright - `pyrightconfig.strict.json`

Strict mode over a curated file allow-list (`include`). Add a file once it is
strict-clean. Everything else is covered only by the repo-wide advisory run.

## Advisory scopes

### mypy advisory - `src/`

`uv run mypy src || true` runs before the gated zone. It carries a
pre-existing backlog, so its exit status is discarded, with one exception: if
the output contains `errors prevented further checking`, mypy aborted before
checking the tree and the step fails. That distinction matters - an aborted run
reports few errors for the same reason an unopened file reports none.

The step prints the advisory error count as a number, both as a `::notice::`
annotation and in the job summary:

| run | scope | blocking | errors |
|---|---|---|---|
| advisory | `src/` (`[tool.mypy]`) | no | *N* |
| gated zone | `mypy.gate.ini` `files` minus `exclude` | yes | must be 0 |

The count is the observable: it is expected to fall as modules leave `exclude`,
and a jump upward is visible in the job summary without changing what gates.

### pyright advisory - repo-wide

The `Type check report` job runs repo-wide pyright in the basic mode configured
by `[tool.pyright]`, discards the exit status, and reports the summary counts
in the job summary. The job is **not** in `ci-gate`'s `needs` list, so it
cannot block a merge in any state.

## Reading a type failure

| Symptom | Meaning |
|---|---|
| `mypy strict (lineage substrate)` red, gated-zone step failed | A real error inside `mypy.gate.ini`'s zone. Fix it or the PR does not merge |
| `mypy strict (lineage substrate)` red, parse step failed | mypy aborted before checking anything - the reported error count is meaningless until this is fixed |
| Advisory error count moved, everything green | An error landed (or was fixed) outside every blocking scope. No gate reacts; the number in the job summary is the only signal |
| A file has type errors and CI is green | Expected when the file is outside both blocking scopes. Promote it (above) if it should gate |

## Promoting a path

1. Fix the errors in the file.
2. Remove its entry from `mypy.gate.ini`'s `exclude`, or add it to
   `pyrightconfig.strict.json`'s `include`.
3. Push. The blocking job now covers it; no workflow change is involved.

## Related

- `docs/operations/ci.md` - CI runbook, including the gate-evaluation guards.
- `docs/operations/merge-gate.md` - the merge-gate layers themselves.
