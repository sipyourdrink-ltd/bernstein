# 643 — GitLab CI Integration

**Role:** devops
**Priority:** 5 (low)
**Scope:** small
**Depends on:** #613

## Problem

The CI integration only supports GitHub Actions. GitLab has 30M+ users and its own CI system. Without GitLab CI support, Bernstein cannot serve a large segment of the developer market.

## Design

Build GitLab CI integration alongside the existing GitHub Actions support. Create a `.gitlab-ci.yml` template that triggers Bernstein orchestration on pipeline failures. Implement a GitLab CI log parser (extending the CI log parser adapter pattern from #602) that extracts failure reasons from GitLab CI output format. Support GitLab-specific features: merge request comments for results, pipeline status updates, and artifact uploads. The integration should work with both gitlab.com and self-hosted GitLab instances. Provide a setup guide covering GitLab CI variables configuration, runner requirements, and permissions needed.

## Files to modify

- `src/bernstein/adapters/ci/gitlab_ci.py` (new)
- `.gitlab-ci.yml` (new — template)
- `docs/gitlab-ci.md` (new)
- `tests/unit/test_gitlab_ci.py` (new)

## Completion signal

- `.gitlab-ci.yml` template triggers Bernstein on pipeline failure
- GitLab CI logs parsed correctly for failure diagnosis
- Works with both gitlab.com and self-hosted instances
