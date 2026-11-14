# Evolve

`bernstein evolve` turns what recent runs did into tracked, actionable items. Its subcommands are:

- **`bernstein evolve run`** - scan run history, surface failure patterns, and (with `--github`) file or update the issue that tracks each one.
- **`bernstein evolve review`** - list upgrade proposals waiting on a human, including the change contract component and sandbox verdict for each.
- **`bernstein evolve approve <PROPOSAL_ID>`** - approve one proposal.
- **`bernstein evolve status`** - show the evolution history table.
- **`bernstein evolve export [OUTPUT]`** - write a static HTML or Markdown report.

`bernstein evolve run` needs an initialised workspace. In a directory with no `.sdd/`, it exits before starting rather than creating state behind your back.

## Exporting a report

`bernstein evolve export [OUTPUT]` reads evolution data from the project selected by `--dir` (the project root containing `.sdd/`). The optional `OUTPUT` argument selects the report's path; it is independent of `--dir`. `OUTPUT` defaults to `evolution_report` and `--format` defaults to HTML. With both omitted, the command writes `evolution_report.html` in the current working directory. Relative output paths are also resolved from the current working directory, not from the project directory selected with `--dir`.

Use `--format html` for HTML or `--format md` / `--format markdown` for Markdown (the aliases are case-insensitive). The selected format determines the renderer. A recognized `.html` or `.md` output suffix is retained; otherwise the selected format supplies the `.html` or `.md` suffix. Thus the suffix does not select the renderer.

For example, from a directory separate from the project, this reads the project at `sample-project` and writes `evolution-summary.md` in the current working directory:

```sh
bernstein evolve export evolution-summary --format markdown --dir sample-project
```

If the selected project has no recorded evolution cycles, the command exits with status 1 and does not write a report.

## The run-ledger to draft to issue contour

Evolve's failure signal is the run ledger, not a separate detector. Three stages, each one a pure step over the previous stage's output:

| Stage | Input | Output |
|---|---|---|
| Classify | every run directory under `.sdd/runtime/ledger` | one classified row per finished run - the same rows [`bernstein runs report`](../cli-reference.md) prints |
| Group | the classified rows | one **failure-pattern draft** per distinct failure signature, each with a fingerprint |
| Sink | the drafts | one GitHub issue per fingerprint: created if new, commented on if it already exists |

### Stage 1 - classify

Every finished run is classified from its ledger into one outcome:

| Outcome | Meaning | Candidate for a draft |
|---|---|---|
| `pr-opened` | the run opened a pull request | no |
| `no-changes` | the run produced no commits over base | no |
| `gate-failed` | a quality gate blocked the run | yes |
| `infra-error` | the adapter or transport died under the run | yes |
| `wedged` | the run closed with tasks still unspawnable | yes |

Only the three failure outcomes feed the next stage. A successful run never contributes to a draft.

### Stage 2 - group into fingerprinted drafts

Failure rows are grouped by their failure signature - the outcome plus the evidence line the classifier recorded. Each group becomes one draft carrying:

- a **fingerprint**: a SHA-256 digest of the signature,
- a title and a body naming the evidence, the occurrence count, and the most recent contributing run,
- the full list of contributing run ids.

The fingerprint depends only on the signature, so it is stable across scans. Scanning the same unchanged ledgers twice produces the identical set of fingerprints - a second pass surfaces nothing new. A different gate name or a different error kind is a different signature, and gets its own fingerprint.

### Stage 3 - the issue sink

With `--github`, each draft is reconciled against the open issues labelled `bernstein-evolve`:

- **New fingerprint** - a new issue is created. It carries the `bernstein-evolve` and `auto-generated` labels plus an `evolve-fingerprint-<hex>` label derived from the fingerprint.
- **Known fingerprint** - the issue already carrying that label gets a comment naming the new occurrence count, the most recent run, and the contributing runs. No second issue is filed.

Issue identity is the fingerprint label, never the title text. The title carries the occurrence count, which changes every time the pattern recurs; keying on it would file a fresh issue on every cycle. Keying on the fingerprint means a failure that recurs ten times updates one issue ten times.

Evolve stops at the tracked issue. It does not open a pull request for what it filed - the issue goes through the same review contour as any other issue in the repository.

## Dry run

`--dry-run` scans for failure-pattern drafts, prints the results, and skips the evolution loop. It is **not necessarily preview-only**: if GitHub sync is enabled, each discovered draft is also synced. A new fingerprint can create an issue; a fingerprint matching an existing issue can add a comment to it. These are remote writes, even with `--dry-run`.

```bash
bernstein evolve run --dry-run
```

The run is preview-only when the effective GitHub sync setting is off: neither `--github` is passed nor `evolve.github_sync: true` is set in the project's `bernstein.yaml` (or `bernstein.yml`). For example, this seed setting enables sync during a dry run:

```yaml
evolve:
  github_sync: true
```

When sync is enabled, the following commands can create issues or comments for discovered drafts; `--github-repo` selects the target repository but does not itself enable sync:

```bash
bernstein evolve run --dry-run --github
bernstein evolve run --dry-run --github --github-repo owner/repo
```

If the scan finds no drafts, Evolve prints “No failure-pattern drafts found” and returns without syncing anything. When sync is enabled and drafts are found, the `gh` CLI must be on `PATH` and authenticated. Otherwise the sync step is skipped with a warning rather than failing the command; run `gh auth login` first.

## Options on `evolve run`

| Flag | Default | Purpose |
|---|---|---|
| `--window` | `2h` | Evolution window duration (`2h`, `30m`, `1h30m`). |
| `--max-proposals` | `24` | Maximum proposals to evaluate per session. |
| `--cycle` | `300` | Seconds per experiment cycle. |
| `--dir` | `.` | Project root - the parent of `.sdd/`. |
| `--github` | off | Reconcile drafts and proposals against GitHub issues. |
| `--github-repo` | inferred | Repo slug (`owner/repo`); inferred from the git remote when omitted. |
| `--dry-run` | off | Print failure-pattern drafts and exit without the loop; GitHub sync still applies if enabled. |

The project seed's `evolve:` block may set `github_sync: true` and `github_repo: owner/repo`; `--github-repo` selects the target repository when supplied on the command line.

## What evolve does not write

Evolve does not apply configuration changes to `policies.yaml`, `routing.yaml`, or `providers.yaml`. A `pending_upgrades:` key left in one of those files by an older version is ignored at load, with one warning naming the file; nothing removes it for you, and nothing reads it.
