# E26 — Plugin Registry

**Priority:** P2
**Scope:** small (10-20 min)
**Wave:** 2 — Ecosystem & Integrations

## Problem
There is no centralized way for users to discover and install Bernstein plugins, forcing manual PyPI searches and guesswork.

## Solution
- Create a `registry.json` file in the repo root containing plugin metadata (name, description, PyPI package name, version, author, tags).
- Implement `bernstein plugin search <keyword>` that fetches the registry JSON from GitHub raw URL and filters entries by keyword match on name/description/tags.
- Implement `bernstein plugin install <name>` that looks up the PyPI package name from the registry and runs `pip install <package>`.
- Add caching of the registry JSON locally with a 1-hour TTL to avoid repeated fetches.

## Acceptance
- [ ] `bernstein plugin search orchestrate` returns matching plugins from the registry
- [ ] `bernstein plugin install <name>` installs the correct PyPI package
- [ ] Registry JSON schema is documented with at least one example entry
- [ ] Searching with no matches prints a helpful "no results" message
