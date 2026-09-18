# bernstein — agent entrypoint

Generated from commit `1567ca0397b6`. Every row below is anchored to `path:line`; an anchor that no longer matches means the row is stale.

| Doc | Answers |
|---|---|
| [10-map.md](./10-map.md) | where the code is, who owns it |
| [20-commands.md](./20-commands.md) | how to run, test, lint |
| [30-ci.md](./30-ci.md) | what must be green before merge |
| [40-env.md](./40-env.md) | which env vars the code reads |
| [50-invariants.md](./50-invariants.md) | what you must not touch |
| `.machine/facts.json` | the same facts, machine-readable |

## Golden commands

| Command | Kind | Anchor |
|---|---|---|
| `ruff check .` | inferred | `pyproject.toml:552` |
| `pytest` | inferred | `pyproject.toml:796` |
| `mypy .` | inferred | `pyproject.toml:672` |

## Rules for agents editing this repo

1. Facts here are anchored. If an anchor no longer matches, the doc is stale — regenerate it rather than trusting it.
2. `kind: inferred` means agent-docs deduced the command from config, not from an explicit declaration. Confirm before relying on it.
3. Sections marked *Nothing detected* were searched for and not found. That is a fact, not a gap to fill with a guess.
