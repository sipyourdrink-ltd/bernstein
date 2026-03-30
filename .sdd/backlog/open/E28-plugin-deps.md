# E28 — Plugin Dependencies in Config

**Priority:** P2
**Scope:** small (10-20 min)
**Wave:** 2 — Ecosystem & Integrations

## Problem
Users must manually install plugins before running Bernstein, leading to "plugin not found" errors and friction in team onboarding.

## Solution
- Add a `plugins:` section to `bernstein.yaml` schema (list of plugin names with optional version pins).
- On `bernstein run`, check if each listed plugin is importable; if not, run `pip install --quiet <plugin>` before proceeding.
- Log installed plugins at debug level so users can troubleshoot.
- Support version pinning syntax: `bernstein-slack>=1.0,<2.0`.

## Acceptance
- [ ] `bernstein.yaml` with a `plugins:` section is parsed without errors
- [ ] Missing plugins are auto-installed on run
- [ ] Already-installed plugins are skipped without re-installing
- [ ] Version-pinned plugins install the correct version
