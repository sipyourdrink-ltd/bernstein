# Click CLI

The top-level `bernstein` command (`pyproject.toml` `[project.scripts]`
maps it to `bernstein.cli.main:cli`). `main.py` builds the root click
group and registers every subcommand; implementations live in
`commands/` (one module per command or group).

## Key files

| File | Purpose |
|---|---|
| `main.py` | Root click group; all `cli.add_command(...)` registrations |
| `commands/` | Command and group implementations |
| `helpers.py` | Shared console and utility helpers |
| `run_cmd.py` | `bernstein run` entry; bootstrap/confirm split into siblings |

## Invariants

- Keep heavy imports lazy inside command bodies so CLI startup stays
  fast; `commands/agents_md_cmd.py` documents the pattern.
- Legacy flat import paths (`bernstein.cli.<module>`) are served by a
  meta-path finder in `__init__.py`; when moving a module, extend the
  redirect rather than leaving a shim file behind.
- The CLI surface is tracked for reduction (issue #3147): prefer a
  subcommand on an existing group over a new top-level command.
- Non-zero exit codes are operator contract; declare them as named
  constants and document them in the command docstring
  (`commands/resume_cmd.py` is the model).

## Testing

Single files only, e.g.
`uv run pytest tests/unit/test_agents_md_cmd.py -x -q`; most commands
have a matching `test_<name>_cmd.py` under `tests/unit/`.

<!-- Reviewed 2026-08-18 against this subtree; the notes above still hold. -->
