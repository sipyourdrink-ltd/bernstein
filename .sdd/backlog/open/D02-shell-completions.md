# D02 — Shell Completions Generator

**Priority:** P1
**Scope:** small (10-20 min)
**Wave:** 1 — Developer Love

## Problem
Users must type full command names and flags from memory. Without shell completions, discoverability of subcommands and options is poor, slowing down power users.

## Solution
- Add a `bernstein completions` command group with two subcommands
- `bernstein completions show --shell bash|zsh|fish` prints the completion script to stdout
- `bernstein completions install --shell bash|zsh|fish` writes the completion script to the appropriate shell config location (e.g., `~/.bashrc`, `~/.zshrc`, fish completions directory)
- Use Click's built-in `shell_complete` / `_BERNSTEIN_COMPLETE` environment variable mechanism to generate completion scripts
- Auto-detect the current shell from `$SHELL` if `--shell` is not provided
- Print a confirmation message after install with instructions to reload the shell

## Acceptance
- [ ] `bernstein completions show --shell bash` outputs a valid bash completion script
- [ ] `bernstein completions show --shell zsh` outputs a valid zsh completion script
- [ ] `bernstein completions show --shell fish` outputs a valid fish completion script
- [ ] `bernstein completions install` without `--shell` auto-detects the current shell
- [ ] After installing completions, pressing Tab after `bernstein ` lists available subcommands
- [ ] After installing completions, pressing Tab after `bernstein run --` lists available flags
