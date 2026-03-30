# E27 — Plugin Scaffold Generator

**Priority:** P2
**Scope:** small (10-20 min)
**Wave:** 2 — Ecosystem & Integrations

## Problem
Creating a new Bernstein plugin from scratch requires boilerplate setup that slows down plugin authors and leads to inconsistent plugin structures.

## Solution
- Implement `bernstein plugin create <name>` command.
- Generate a plugin directory with: `pyproject.toml` (with bernstein plugin entry point), `src/<name>/hooks.py` (with example lifecycle hooks: `on_task_start`, `on_task_complete`, `on_error`), `README.md` with usage instructions.
- Use simple string templating (f-strings or `string.Template`) rather than pulling in cookiecutter as a dependency.
- Include a `.gitignore` and basic test file in the scaffold.

## Acceptance
- [ ] `bernstein plugin create my-plugin` creates a valid Python package directory
- [ ] Generated `pyproject.toml` includes the correct entry point for plugin discovery
- [ ] `hooks.py` contains working example lifecycle hook functions
- [ ] Generated package can be installed with `pip install -e .` without errors
