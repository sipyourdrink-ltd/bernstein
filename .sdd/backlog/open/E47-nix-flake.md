# E47 — Nix Flake

**Priority:** P2
**Scope:** small (10-20 min)
**Wave:** 2 — Ecosystem & Integrations

## Problem
Nix users cannot install Bernstein through the Nix package manager, missing out on reproducible and declarative installation.

## Solution
- Create a `flake.nix` in the repository root.
- Define a package output that builds bernstein from source using `buildPythonPackage`.
- Support direct execution: `nix run github:chernistry/bernstein -- -g "my goal"`.
- Include a dev shell with bernstein and development dependencies.
- Add Nix installation instructions to the README.

## Acceptance
- [ ] `nix build` produces a working bernstein binary
- [ ] `nix run github:chernistry/bernstein -- --help` prints usage
- [ ] `nix develop` enters a shell with bernstein and dev dependencies available
- [ ] `flake.lock` is committed and reproducible
