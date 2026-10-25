# Click CLI

The top-level `bernstein` command (`pyproject.toml` `[project.scripts]` maps it to `bernstein.cli.main:cli`). `main.py` builds the root click group and registers top-level commands and groups; implementations live in `commands/` (one module per command or group).

## Key files

| File | Purpose |
|---|---|
| `main.py` | Root click group; registers top-level commands and groups. Nested subcommands are registered on their parent group |
| `commands/` | Command and group implementations |
| `helpers.py` | Shared console and utility helpers |
| `run_cmd.py` | `bernstein run` entry; bootstrap/confirm split into siblings |
| `commands/governance_cmd.py` | The `bernstein govern` group: `verify`, `plan`, and the `audit`, `reconcile`, `inventory` and `lane` subcommands that live in `govern_*_cmd.py` |
| `commands/trust_cmd.py` | `bernstein trust`: the one sanctioned writer of `.sdd/runtime/workspace_trust.json` |
| `commands/verify_cmd.py`, `commands/verify_kinds.py` | `bernstein verify <artefact-path>` dispatches on the artefact's kind through a registry (#5103) |
| `commands/seal_cmd.py` | `bernstein seal`: anchors a run's sealed head outside this install (#4205) |
| `commands/identity_review_cmd.py` | `bernstein identity review` access review commands, registered by `commands/identity_cmd.py` |
| `commands/identity_attest_cmd.py` | `bernstein identity attest` run attestation CLI commands |
| `commands/insights_cmd.py` | Registered `bernstein insights` skeleton; echoes the requested window as text or JSON without computing analytics |

## Invariants

- Keep heavy imports lazy inside command bodies so CLI startup stays fast; `commands/agents_md_cmd.py` documents the pattern.
- Legacy flat import paths (`bernstein.cli.<module>`) are served by a meta-path finder in `__init__.py`; when moving a module, extend the redirect rather than leaving a shim file behind.
- The CLI surface is tracked for reduction (issue #3147): prefer a subcommand on an existing group (`govern`, `identity`, `adapters`, `worktrees`) over a new top-level command.
- Non-zero exit codes are operator contract; declare them as named constants and document them in the command docstring (`commands/resume_cmd.py` is the model).
- Repository-authored commands (plugin and lifecycle hooks, workflow command nodes) run only in a trusted workspace. Nothing grants trust implicitly; only `bernstein trust` writes the record (`commands/trust_cmd.py`).

## Testing

Single files only, e.g. `uv run pytest tests/unit/test_agents_md_cmd.py -x -q`; most commands have a matching `test_<name>_cmd.py` under `tests/unit/`, and newer ones sit under `tests/unit/cli/`.

<!-- Reviewed 2026-10-24 against main b80e0aa71ee7; the notes above still hold. -->
