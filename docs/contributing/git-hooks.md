# Local Git hooks

From the root of a fresh clone, follow the
[contributor setup](https://github.com/sipyourdrink-ltd/bernstein/blob/main/CONTRIBUTING.md#quick-start)
before installing optional Git hooks.

## Pre-commit

Run the checks from `.pre-commit-config.yaml` against staged files:

```bash
uvx --python 3.13 pre-commit run
```

For a persistent hook, install the tool outside the uv cache and then
install the hook from the repository root:

```bash
uv tool install --python 3.13 pre-commit
pre-commit install
```

If the command is not on your PATH, run `uv tool update-shell` and open a
new shell before installing the hook. Run these commands on each clone
where you want the hook.

The checks may edit staged files for formatting and whitespace. Review
and stage those edits before retrying the commit. To check explicit
paths, pass `run --files` followed by the paths.

## Pre-push

Install the versioned hook on a standard clone:

```bash
ln -s ../../scripts/git-hooks/pre-push .git/hooks/pre-push
```

The hook blocks pushes for Ruff lint and import-contract failures.
Pyright results are advisory. Run Git with `UV_PYTHON=3.13` to match
the contributor environment.

## Existing `bernstein install-hooks` installations

This command writes its own `pre-commit` and `pre-push` scripts under
`.git/hooks/`. Its pre-commit runs Ruff with fixes and runs
`pytest tests/unit` in one process. Its pre-push is a no-op. The
recommended hooks above use the repository's check configuration and
keep the full unit suite in the isolated test runner. If migrating,
inspect and back up any existing hooks before replacing them.
`bernstein install-hooks --force` writes through a pre-push symlink and
can overwrite the tracked `scripts/git-hooks/pre-push` file.

These local checks are separate from the
[lifecycle hooks contract](hooks.md) and the
[hook permission-rule prefilter](../operations/hooks.md).
