# E35 — Jenkins Shared Library

**Priority:** P2
**Scope:** small (10-20 min)
**Wave:** 2 — Ecosystem & Integrations

## Problem
Jenkins users need a reusable shared library to invoke Bernstein from Jenkinsfiles without duplicating shell script boilerplate across projects.

## Solution
- Create a Jenkins Shared Library at `ci-templates/jenkins/` with standard directory structure: `vars/bernstein.groovy` (global step), `src/` (optional helper classes), `resources/`.
- `bernstein.groovy` provides a `bernstein(goal:, config:, apiKey:)` step that shells out to the bernstein CLI.
- Include example Jenkinsfiles: declarative pipeline and scripted pipeline.
- Handle errors: capture exit code, mark build as failed if bernstein returns non-zero.

## Acceptance
- [ ] `vars/bernstein.groovy` defines a callable pipeline step
- [ ] Step correctly executes `bernstein run -g <goal>` via shell
- [ ] Non-zero exit codes from bernstein fail the Jenkins build
- [ ] Example Jenkinsfiles demonstrate both declarative and scripted usage
