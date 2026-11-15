# Shell completions

`bernstein completions` prints a shell-specific completion script for the
`bernstein` CLI, generated from Click's built-in completion support. The script
connects the shell to the live root command tree; it does not print a fixed list
of suggestions. Only commands available for completion are suggested (hidden
commands are not advertised).

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

**fish** — load the generated completion into the current shell:

```fish
bernstein completions --shell fish | source
```

To save the script for subsequent fish sessions, write it to a file first:

```fish
bernstein completions --shell fish > bernstein.fish
```

Move `bernstein.fish` to fish's completions directory,
`~/.config/fish/completions/bernstein.fish` (create the directory if needed).
Fish loads completion files from that directory in future shells.

## How it behaves

The command walks up from its own Click context to the root CLI group, so
completion is built for the full command tree, not just the `completions`
command itself. It uses Click's `BashComplete` / `ZshComplete` / `FishComplete`
classes rather than a hand-maintained list of subcommands. Once the generated
script is loaded, the shell invokes the installed `bernstein` CLI through
Click's `_BERNSTEIN_COMPLETE` environment-variable protocol when the user asks
for completions; the CLI then returns suggestions for the current command and
input. Newly added commands and options can therefore be completed without
changes to this command.

## Source

`src/bernstein/cli/commands/completions_cmd.py` (`completions_cmd`,
registered as `bernstein completions`).
