# E48 — Scoop Manifest

**Priority:** P2
**Scope:** small (10-20 min)
**Wave:** 2 — Ecosystem & Integrations

## Problem
Windows users cannot install Bernstein through Scoop, the popular Windows command-line installer, making onboarding harder on Windows.

## Solution
- Create a Scoop manifest file `bernstein.json` at `packaging/scoop/bernstein.json`.
- Define the manifest with: version, URL to the release archive, SHA256 hash, binary path, and dependencies (Python).
- Support `scoop install bernstein` by submitting to the `scoop-extras` bucket.
- Add an auto-update section in the manifest using GitHub release URL patterns.
- Include instructions for testing the manifest locally before submitting.

## Acceptance
- [ ] `bernstein.json` is a valid Scoop manifest
- [ ] `scoop install bernstein` (via local bucket) installs bernstein correctly
- [ ] Manifest includes auto-update configuration for new releases
- [ ] SHA256 hash matches the release archive
