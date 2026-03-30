# E31 — GitLab CI Template

**Priority:** P2
**Scope:** small (10-20 min)
**Wave:** 2 — Ecosystem & Integrations

## Problem
GitLab CI users have no ready-made template for integrating Bernstein into their pipelines, requiring manual pipeline configuration from scratch.

## Solution
- Create a `.gitlab-ci.yml` template file at `ci-templates/gitlab/.gitlab-ci.yml`.
- Template installs bernstein via pip, runs `bernstein run -g "$GOAL"` with configurable variables.
- Define CI variables: `BERNSTEIN_GOAL`, `BERNSTEIN_CONFIG`, `BERNSTEIN_API_KEY`.
- Publish as a GitLab CI template that teams can reference with `include: remote:`.
- Add usage documentation with example `include:` snippet.

## Acceptance
- [ ] Template is valid GitLab CI YAML that passes `gitlab-ci-lint`
- [ ] Teams can include the template with a single `include:` directive
- [ ] CI variables are documented and configurable
- [ ] Template runs bernstein successfully in a GitLab CI pipeline
