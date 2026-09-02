# Evolve

`bernstein evolve` turns what recent runs did into tracked, actionable items. Its subcommands are:

- **`bernstein evolve run`** - scan run history, surface failure patterns, and (with `--github`) file or update the issue that tracks each one.
- **`bernstein evolve review`** - list upgrade proposals waiting on a human.
- **`bernstein evolve approve <PROPOSAL_ID>`** - approve one proposal.
- **`bernstein evolve status`** - show the evolution history table.
- **`bernstein evolve export <OUTPUT>`** - write a static HTML or Markdown report.

`bernstein evolve run` needs an initialised workspace. In a directory with no `.sdd/`, it exits before starting rather than creating state behind your back.

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

`--dry-run` prints what the scan found and performs no remote writes at all:

```bash
bernstein evolve run --dry-run
```

It prints one row per draft - fingerprint prefix, pattern title, occurrence count, most recent run id - and returns without starting the evolution loop. No `gh` subprocess is spawned.

To preview what the sink would do against a real tracker, combine the flags:

```bash
bernstein evolve run --dry-run --github
bernstein evolve run --dry-run --github --github-repo owner/repo
```

`--github` requires the `gh` CLI on `PATH` and authenticated. If it is not, the sync step is skipped with a warning rather than failing the command; run `gh auth login` first.

## Options on `evolve run`

| Flag | Default | Purpose |
|---|---|---|
| `--window` | `2h` | Evolution window duration (`2h`, `30m`, `1h30m`). |
| `--max-proposals` | `24` | Maximum proposals to evaluate per session. |
| `--cycle` | `300` | Seconds per experiment cycle. |
| `--dir` | `.` | Project root - the parent of `.sdd/`. |
| `--github` | off | Reconcile drafts and proposals against GitHub issues. |
| `--github-repo` | inferred | Repo slug (`owner/repo`); inferred from the git remote when omitted. |
| `--dry-run` | off | Print failure-pattern drafts and exit without running the loop. |

`--github` and `--github-repo` also read from an `evolve:` block in `bernstein.yaml` when the flag is not passed on the command line.

## What evolve does not write

Evolve does not apply configuration changes to `policies.yaml`, `routing.yaml`, or `providers.yaml`. A `pending_upgrades:` key left in one of those files by an older version is ignored at load, with one warning naming the file; nothing removes it for you, and nothing reads it.
