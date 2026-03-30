# E49 — Auto-Updater

**Priority:** P2
**Scope:** small (10-20 min)
**Wave:** 2 — Ecosystem & Integrations

## Problem
Users must manually check for and install Bernstein updates, leading to version drift across teams and missed bug fixes.

## Solution
- Implement `bernstein self-update` command that checks PyPI for the latest version.
- Compare the installed version with the latest available; if newer, install it via `pip install --upgrade bernstein`.
- Show a changelog diff between the current and latest versions (fetched from GitHub releases).
- Implement `bernstein self-update --rollback` that reverts to the previously installed version.
- Store the previous version path in `~/.bernstein/previous-version` for rollback support.

## Acceptance
- [ ] `bernstein self-update` upgrades to the latest version from PyPI
- [ ] Changelog diff is displayed before confirming the update
- [ ] `bernstein self-update --rollback` reverts to the previous version
- [ ] If already on the latest version, a "you're up to date" message is shown
