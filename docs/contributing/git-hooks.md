# `bernstein install-hooks`

Installs local git pre-commit and pre-push hooks so common checks run
before code leaves your machine, instead of failing for the first time
in CI.

This is unrelated to the [lifecycle hooks contract](hooks.md)
(`pre_task`/`post_task`/`pre_merge`/etc., driven by `bernstein.yaml` and
run by the orchestrator against agent activity) and to the
[hook permission-rule prefilter](../operations/hooks.md) (the `if:`
filter on those lifecycle hooks). `install-hooks` writes plain git
hooks into `.git/hooks/` for the contributor's own commits.

## Usage

```bash
bernstein install-hooks
bernstein install-hooks --force   # -f: overwrite hooks that already exist
```

| Flag | Meaning |
|---|---|
| `--force`, `-f` | Overwrite an existing `pre-commit` or `pre-push` hook. Without it, an existing hook is left untouched and skipped. |

The command exits with an error if run outside a git repository (no
`.git/hooks` directory).

## What it installs

Two scripts are written to `.git/hooks/` and made executable
(`chmod 755`):

**`pre-commit`**

```bash
#!/bin/bash
set -e
uv run ruff check --fix .
uv run pytest tests/unit -x -q
```

Runs `ruff check --fix` and the unit test suite (stopping at the first
failure) before a commit is allowed to complete.

**`pre-push`**

```bash
#!/bin/bash
# Check for unmerged PRs or blocked status before push
exit 0
```

Currently a placeholder that always exits `0` (i.e. never blocks a
push).

For each of the two hooks, if the target file already exists and
`--force` was not passed, the command prints a note and leaves that
hook untouched — it does not overwrite silently and does not merge
with an existing hook script.

## Limitations

- The installed `pre-commit` hook runs the full `tests/unit` suite on
  every commit; on a large working tree this can be slower than
  committing without it.
- Hooks are written to `.git/hooks/`, which git does not version —
  each clone/worktree must run `bernstein install-hooks` again to get
  them.
- There is no `uninstall-hooks` command; remove the files from
  `.git/hooks/` directly to revert.

## Source

- `src/bernstein/cli/commands/advanced_cmd.py` — `install_hooks` (registered as `bernstein install-hooks` in `cli/main.py`)
