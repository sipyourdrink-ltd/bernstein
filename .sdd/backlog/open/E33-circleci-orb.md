# E33 — CircleCI Orb

**Priority:** P2
**Scope:** small (10-20 min)
**Wave:** 2 — Ecosystem & Integrations

## Problem
CircleCI users lack a reusable orb for Bernstein, forcing them to write repetitive pipeline configuration for each project.

## Solution
- Create a CircleCI Orb at `ci-templates/circleci/` with orb YAML configuration.
- Define commands: `install` (installs bernstein), `run` (executes `bernstein run -g <goal>`).
- Define a job `orchestrate` that combines install and run steps.
- Support parameters: `goal`, `config-file`, `python-version`.
- Add publishing instructions for the CircleCI Orb Registry under `bernstein/orchestrate`.

## Acceptance
- [ ] Orb YAML passes `circleci orb validate`
- [ ] `bernstein/orchestrate` job can be used in a CircleCI config
- [ ] `bernstein run -g <goal>` parameter is configurable
- [ ] README includes example CircleCI config using the orb
