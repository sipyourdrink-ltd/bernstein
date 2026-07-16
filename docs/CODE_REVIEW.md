# Code Review Process

Last reviewed: 2026-07-16.

This document describes how changes land in Bernstein. It exists so the OpenSSF
Scorecard, regulated buyers, and new contributors can all read the same answer
in one place.

## Default rule

All non-trivial changes land via pull request against `main` with at least one
approving review from a maintainer or designated reviewer. Direct pushes to
`main` are reserved for the operator (repo owner) and limited to:

- Release-cut commits produced by the auto-release workflow.
- Documentation typo fixes and link repairs.
- Emergency hotfixes for production-breaking issues, which are followed by a
  retrospective PR within 24 hours.

## Security-sensitive changes

Changes that touch any of the following paths require **two** approving
reviews, or operator-only push from a verified-signed commit:

- `SECURITY.md`
- `.github/workflows/**`
- `src/bernstein/core/security/**`
- `src/bernstein/core/identity/**`
- Anything under `*signing*`, `*auth*`, `*credential*`, or `*token*` paths
- Cryptographic key material, public keys, or signature verification logic

## Required status checks

Branch protection on `main` requires the following checks to pass before a PR
can merge:

- CI (`.github/workflows/ci.yml`)
- CodeQL (`.github/workflows/codeql.yml`)
- SonarQube quality gate (self-hosted; `sonar-scan.yml` `workflow_run` on CI)
- Architecture contracts (`lint-imports`)
- Type checks (`pyright src/`)

A green status from each is non-negotiable.

## Reviewer expectations

A reviewer is expected to:

1. Pull the branch locally for any non-trivial change and run the affected
   test files.
2. Sanity-check that new public surface (CLI flags, MCP tools, HTTP routes,
   adapter contracts) has corresponding tests and docs in the same PR.
3. Flag any new dependency, new outbound network call, or new credential read
   in the PR conversation before approving.

## Auto-merged PRs

The following PR classes are auto-merged when checks are green and one
approving review is present:

- Dependabot bumps in the `minor-and-patch` and `actions-minor-patch` groups.
- Documentation-only changes (`docs:` prefix) from maintainers.

Security-group Dependabot bumps (`cryptography`, `pyjwt`, `lxml`, etc.) are
never auto-merged; they require a maintainer review even when the diff is
trivial.

## Escalation

If a reviewer disagrees with the operator on a security-sensitive change, the
change is blocked until a second maintainer weighs in or the disagreement is
recorded in `docs/decisions/`. Disagreements about non-security changes are
resolved by the operator after the reviewer's concerns are acknowledged in the
PR conversation.
