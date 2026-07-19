# Docs update sweep playbook

This playbook is the operational companion to `docs/playbooks/docs-drift.md`.
The drift playbook enumerates every doc and points each one at a concrete
source-of-truth code path. This playbook describes how to actually run a
refresh sweep across the inventory, including how to parallelise the work
across multiple agents without merge conflicts.

## When to run this sweep

Run a full sweep on any of the following triggers:

- After a release tag has been cut (release-please merge to `main`).
- After a feature wave lands a set of related PRs that touched multiple
  source-of-truth modules (typically 5+ PRs that each named a module from
  `docs-drift.md`).
- On a weekly cadence as a hygiene pass, independent of the release cycle.
- When `scripts/check_docs_drift.py --strict` reports drift on `main` that
  cannot be attributed to a single in-flight PR.

A partial sweep (one or two partitions only) is acceptable when the trigger
is scoped to a known area, for example, only the GUI partition after a
GUI-only feature wave.

## How to parallelise

The doc inventory in `docs/playbooks/docs-drift.md` is structured into
sections that map cleanly onto partitions. Each partition can be worked by
one agent independently of the others because the file sets do not overlap.

| Partition | Section in drift playbook | Approximate file count |
|-----------|---------------------------|------------------------|
| `root` | `Root (./)` | 9 |
| `docs-top` | `docs/ top-level` | 13 |
| `docs-concepts` | `docs/concepts/` | 19 |
| `docs-gui` | `docs/gui/` | 7 |
| `docs-sdd` | `docs/sdd/` | 1 (plus benchmarks row) |

Recommended split for a full sweep: assign one agent per partition. The
`docs-sdd` partition can be folded into whichever partition the same agent
is already running because the row count is small.

Each agent works on its own branch, opens its own PR, and never touches
files outside its partition. The merge train (release-please or the
operator) handles ordering of PRs.

## Per-partition recipe

For each partition the recipe is the same. The only thing that changes is
the set of rows the agent reads from `docs-drift.md`.

1. Create a fresh branch off `main`:

   ```bash
   git checkout main
   git pull --ff-only
   git checkout -b docs/refresh-<partition>
   ```

2. Read the assigned rows from `docs/playbooks/docs-drift.md`. The rows
   for each partition live under the section header named in the table
   above.

3. For each row in the partition:

   - Read the named source-of-truth code path(s).
   - Read the current doc file.
   - List discrepancies between the doc and the source. A discrepancy is
     any of: stale module path, removed CLI flag, removed module name,
     new module not mentioned, new CLI subcommand not mentioned, schema
     field added or removed, contact or repo URL changed, version number
     changed.
   - Apply the remediation token named in the row.

4. Apply each remediation as follows:

   | Remediation token | How to apply |
   |-------------------|--------------|
   | `agents-md-sync` | `uv run bernstein agents-md sync` then `uv run bernstein agents-md verify`. Operates only on AGENTS.md, CLAUDE.md, CONVENTIONS.md, `.goosehints`, `.cursor/rules/*.mdc`. |
   | `gen-agents-md` | `uv run python scripts/gen_agents_md.py --update`. Only run if the row explicitly names this token; never run for rows that name `agents-md-sync`. |
   | `manual-prose` | Re-read the source-of-truth module(s); edit the doc by hand to reflect current public surface; do not regenerate. |
   | `manual-cmd` | Run `bernstein <cmd> --help` for each CLI surface the doc claims to document; reconcile the listed flags / subcommands by hand. |
   | `gen-benchmarks` | `uv run python scripts/generate_benchmark_docs.py`. |
   | `static` | No code source; check only that repo URL, license, and contact lines are current. |

5. After all rows in the partition are processed, run the drift gate:

   ```bash
   uv run python scripts/check_docs_drift.py --strict
   ```

   The script exits 0 when the doc set matches the named source-of-truth
   paths.

6. Commit the partition. One commit per partition is preferred; if the
   partition is large (`docs-concepts`), split into two commits at a
   natural boundary (for example, orchestration concepts vs persistence
   concepts) but keep them on the same branch.

   Commit message format:

   ```text
   docs(refresh-<partition>): sync to current source-of-truth modules

   - <one line per touched file describing the change>
   ```

   Concrete partition tokens:

   - `docs(refresh-root)`
   - `docs(refresh-concepts)`
   - `docs(refresh-gui)`
   - `docs(refresh-sdd)`
   - `docs(refresh-top)` for the `docs/` top-level partition

7. Push the branch and open a PR titled
   `docs(refresh): <partition> partition per drift playbook`. The PR body
   should list which rows were touched and confirm
   `scripts/check_docs_drift.py --strict` exits 0.

## Conflict avoidance

The partitions are designed so that two agents working in parallel never
touch the same file. Conflict avoidance rules:

- An agent must not edit any file outside its assigned partition. If a
  source-of-truth review surfaces a discrepancy in a file owned by
  another partition, leave it; the next sweep that covers that partition
  picks it up.
- Each agent opens a separate PR. Do not stack PRs unless the operator
  explicitly requests it.
- The merge train (release-please plus the operator) orders the PRs. The
  drift gate runs on every PR and on `main`, so the order of merges does
  not matter as long as each PR is internally consistent.
- If two partitions both name the same source-of-truth file (this should
  not happen by construction; if it does, the drift playbook itself has
  a bug), the partition that owns the doc that lives nearest to that
  source-of-truth file in the directory tree wins. The other partition
  reports the conflict to the operator and skips that row.

## Verification

A sweep is complete only when both of the following hold:

1. `scripts/check_docs_drift.py --strict` exits 0 on every open partition
   branch and on `main` after merge.
2. For partitions that touched data-freshness lines (the `as of YYYY-MM-DD`
   markers documented in `docs-drift.md`),
   `scripts/check_data_freshness.py` runs without hard failure (advisory
   warnings are acceptable).

If either check fails, the partition branch is not ready to merge. Fix the
remaining drift rows on the same branch and re-run the gate.

## Skip rules

The following items are explicitly out of scope for a docs-update sweep:

- `docs/compare/*` is operator-managed and out of scope for a sweep. The
  directory is not present in the repo at the time of writing and the
  drift playbook carries no rows for it, so a sweep has nothing to
  process there. If the directory is reintroduced, treat its contents as
  operator-owned and leave them to an explicit operator request rather
  than a sweep pass.
- `CHANGELOG.md` is managed by release-please via the configuration in
  `release-please-config.json` and `release-please-manifest.json`. Do not
  hand-edit released entries. The `## Unreleased` section, if present,
  may be appended to by a feature-wave PR but is never touched by a docs
  sweep.
- Any doc that names a third-party ecosystem tool in its filename and
  exists only as an integration memo is treated as static for sweep
  purposes; refresh only on explicit operator request.
- Any gitignored or otherwise untracked path is out of scope by
  definition; a sweep only touches files tracked in the repo.

## Operator follow-ups

After the sweep, the operator should:

- Confirm the partition PRs have merged in any order; the drift gate on
  `main` will block if any did not land cleanly.
- Re-run `scripts/check_docs_drift.py --strict` locally on the latest
  `main` to confirm a clean baseline before declaring the sweep done.
- Note the sweep date in the operator log so the weekly cadence can be
  tracked.
