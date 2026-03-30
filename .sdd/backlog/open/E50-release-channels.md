# E50 — Release Channels

**Priority:** P2
**Scope:** small (10-20 min)
**Wave:** 2 — Ecosystem & Integrations

## Problem
All users receive the same release version, with no way to opt into early access for testing or to stay on the bleeding edge for development.

## Solution
- Define three release channels: `stable` (default, published to PyPI), `beta` (pre-release versions on PyPI or GitHub releases), `nightly` (built from latest commit, published as GitHub release).
- Implement `bernstein self-update --channel <channel>` to switch channels.
- Store the selected channel in `~/.bernstein/config.yaml`.
- `stable` installs from PyPI, `beta` installs pre-release from PyPI (`pip install --pre`), `nightly` downloads from GitHub releases.
- Show a warning banner when running beta or nightly versions.

## Acceptance
- [ ] `bernstein self-update --channel beta` installs the latest beta version
- [ ] `bernstein self-update --channel nightly` installs the latest nightly build
- [ ] `bernstein self-update --channel stable` returns to the stable release
- [ ] A warning banner is displayed when running non-stable versions
