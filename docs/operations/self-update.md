# Self-update

`bernstein self-update` upgrades (or rolls back) the installed `bernstein`
package via `pip`, checking PyPI for the latest released version and
showing the relevant GitHub release notes before installing.

## Usage

```bash
bernstein self-update             # check PyPI, show changelog, prompt, upgrade
bernstein self-update --check     # show latest version only, don't install
bernstein self-update --rollback  # revert to the previously installed version
bernstein self-update -y          # upgrade without a confirmation prompt
```

| Flag | Default | Meaning |
|---|---|---|
| `--check` | off | Print the installed vs. latest-on-PyPI version and exit; no install. |
| `--rollback` | off | Reinstall the previously installed version (see below); ignores `--check`. |
| `--yes`, `-y` | off | Skip the `Upgrade X → Y?` confirmation prompt. |

## How it behaves

1. Reads the installed version via `importlib.metadata.version("bernstein")`
   (`"unknown"` if the package metadata can't be found).
2. Queries `https://pypi.org/pypi/bernstein/json` for the latest published
   version. If PyPI can't be reached, the command prints a network-error
   message and exits `1`.
3. Prints an installed/latest version table.
4. With `--check`, stops here.
5. If already up to date, reports so and stops.
6. Otherwise fetches GitHub releases
   (`api.github.com/repos/.../releases`) and prints the body (truncated to
   300 characters) of every release tag strictly between the installed and
   latest version, newest first. A GitHub API failure is swallowed — the
   upgrade still proceeds, just without a changelog.
7. Prompts `Upgrade <current> → <latest>?` unless `--yes` was passed.
8. Before installing, writes the current version to
   `~/.bernstein/previous-version` (skipped if the current version is
   `"unknown"`) — this is the rollback point for `--rollback`.
9. Runs `pip install bernstein==<latest> --quiet` in a subprocess. On
   failure, prints pip's stderr and exits `1`.

### Rollback

`--rollback` reads `~/.bernstein/previous-version`. If that file doesn't
exist (no prior successful upgrade recorded a rollback point), it prints a
message and exits `1`. Otherwise it `pip install`s the recorded version and,
on success, deletes the rollback file — so rollback is a one-shot action:
you can't roll back twice in a row without upgrading again first.

## Limitations

- There's only one rollback slot. Each successful upgrade overwrites
  `~/.bernstein/previous-version` with whatever was installed immediately
  before it — there is no multi-version history.
- All installs go through `pip install <pkg>==<version>`; the command
  doesn't detect or special-case `pipx`, `uv tool`, Homebrew, or other
  installation methods. Restart your shell (or check
  `bernstein --version`) after upgrading to confirm the new version is
  actually the one being invoked.

## Source

`src/bernstein/cli/commands/self_update_cmd.py` (`self_update_cmd`,
registered as `bernstein self-update`).
