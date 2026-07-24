# Shell completions

`bernstein completions` prints a shell completion script for the `bernstein`
CLI, generated from Click's built-in completion support. It reflects the
live command tree — every group and command registered on the root CLI at
the moment the script is generated, including their subcommands.

## Usage

```bash
bernstein completions --shell bash
bernstein completions --shell zsh
bernstein completions --shell fish
```

| Flag | Default | Meaning |
|---|---|---|
| `--shell` | `bash` | One of `bash`, `zsh`, `fish`. |

### Installing

**bash** — add to `~/.bashrc`:

```bash
eval "$(bernstein completions --shell bash)"
```

**zsh** — add to `~/.zshrc`:

```bash
eval "$(bernstein completions --shell zsh)"
```

**fish** — write the script to fish's completions directory:

```bash
bernstein completions --shell fish | source
```

## How it behaves

The command walks up from its own Click context to the root CLI group, so
the generated script always covers the full command tree, not just the
`completions` command itself. It uses Click's `BashComplete` / `ZshComplete`
/ `FishComplete` classes directly (the same mechanism Click's own
`_BERNSTEIN_COMPLETE` environment-variable completion protocol uses under
the hood) rather than a hand-maintained list of subcommands, so newly added
commands and options get tab completion automatically without any change to
this command.

## Source

`src/bernstein/cli/commands/completions_cmd.py` (`completions_cmd`,
registered as `bernstein completions`).
