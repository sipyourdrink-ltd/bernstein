# E34 — Buildkite Plugin

**Priority:** P2
**Scope:** small (10-20 min)
**Wave:** 2 — Ecosystem & Integrations

## Problem
Buildkite users have no plugin for running Bernstein in their build pipelines, requiring custom script steps for every project.

## Solution
- Create a Buildkite Plugin at `ci-templates/buildkite/` with: `plugin.yml` (metadata, configuration schema), `hooks/command` (bash script that installs and runs bernstein).
- Plugin name: `chernistry/bernstein-buildkite-plugin`.
- Support configuration properties: `goal`, `config`, `api-key` (from environment).
- hooks/command script installs bernstein if not present, then runs `bernstein run -g "$GOAL"`.
- Add publishing instructions for the Buildkite Plugin Registry.

## Acceptance
- [ ] `plugin.yml` defines valid configuration schema
- [ ] `hooks/command` script runs bernstein with the configured goal
- [ ] Plugin can be referenced in a Buildkite pipeline YAML
- [ ] README includes a complete pipeline step example
