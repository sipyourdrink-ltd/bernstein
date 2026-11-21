## What

<!-- One-liner: what does this PR do? -->

## Why

<!-- What problem does it solve? Link to issue if applicable. -->

## How

<!-- Brief description of the approach. -->

## Checklist

- [ ] `make lint` passes (Ruff lint and format checks)
- [ ] `make typecheck` passes (strict Pyright)
- [ ] `make test TEST_ARGS=-x` passes
- [ ] New code has type hints

### Documentation duty (every PR that touches a feature)

- [ ] User-visible README section updated (or N/A if internal-only)
- [ ] `docs/operations/<area>.md` updated (or N/A)
- [ ] `docs/api/` schema regenerated if a public surface changed (or N/A)
- [ ] `uv run bernstein agents-md sync` run so AGENTS.md, CLAUDE.md, `.goosehints`, `CONVENTIONS.md`, and `.cursor/rules/*.mdc` reflect any new module (or N/A)
- [ ] Tests cover the documented behaviour
