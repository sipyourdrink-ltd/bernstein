# Release Operations

This page documents which GitHub Actions workflows own each release entrypoint.

Update this table whenever a release workflow is added, renamed, or moved.

## Release Workflow Ownership

| Workflow | Name | Triggers | Owns | Handoff |
|---|---|---|---|---|
| `.github/workflows/post-ci-dispatcher.yml` | Post-CI dispatcher | `workflow_run` | Routes completed main-branch CI runs to release and recovery child workflows. | Calls `.github/workflows/auto-release.yml` when the upstream CI run targets `main`. |
| `.github/workflows/auto-release.yml` | Auto-release | `workflow_call` | Decides whether a green main-branch CI run should create a release tag. | Pushes a `v*` tag; `.github/workflows/publish.yml` owns tag publish. |
| `.github/workflows/publish.yml` | Publish | `push` | Builds release distributions, attests `dist/*`, publishes PyPI and npm packages, and creates or updates the GitHub Release for a `v*` tag. | Dispatches the Docker, Homebrew, and SBOM follow-up workflows after creating the release, because a release created with `GITHUB_TOKEN` emits no `release: published` event. |
| `.github/workflows/release-major-minor.yml` | Major/Minor Release | `workflow_dispatch` | Manually cuts major or minor releases after checking CI and applying the version bump. | Pushes the version commit and tag, then builds and publishes from the same run. |
| `.github/workflows/reconcile-release.yml` | Reconcile release drift | `schedule`, `workflow_dispatch` | Compares `pyproject.toml` against PyPI, npm, the Homebrew tap, and GitHub Release assets (dist + SBOM) to detect missed publish work. | Opens or updates a `release-drift` issue naming the channels that are behind. |
| `.github/workflows/publish-docker.yml` | Publish Docker Image | `release`, `workflow_dispatch` | Publishes the GHCR image and image provenance for a released tag. | Dispatched by `publish.yml` for automated releases; the `release` event covers releases created in the UI. |
| `.github/workflows/publish-homebrew.yml` | Publish Homebrew Formula | `release`, `workflow_dispatch` | Updates the Homebrew tap formula for a released version. | Dispatched by `publish.yml` for automated releases; the `release` event covers releases created in the UI. |
| `.github/workflows/sbom.yml` | SBOM | `release`, `workflow_dispatch` | Generates SPDX + CycloneDX SBOMs and attaches them to the release that exists for the built ref. | Dispatched by `publish.yml` for automated releases; the `release` event covers releases created in the UI. |

## Bumping the version

`scripts/bump_version.py` is the only supported way to bump the release version.

```
python scripts/bump_version.py 3.4.5
```

It performs the three coupled edits in one deterministic step:

1. rewrites `project.version` in `pyproject.toml`,
2. runs `uv lock` so `uv.lock` pins the new version, and
3. regenerates `server.json` and `.plugin/plugin.json` via
   `scripts/gen_distribution_manifests.py`.

Do not hand-edit any of these files for a bump. A bump that touches only
`pyproject.toml` desyncs the lockfile and the distribution manifests, which the
CI drift gates then fail. Commit the bump in a PR; the merge to `main` triggers
CI and `.github/workflows/auto-release.yml` picks up the untagged version.

## Guardrails

- `.github/workflows/auto-release.yml` only creates tags.
- `.github/workflows/publish.yml` owns tag-triggered package and GitHub Release publication.
- `.github/workflows/reconcile-release.yml` is the drift detector for every published channel: PyPI, npm, the Homebrew tap, and the GitHub Release's dist and SBOM assets.
- Events raised with `GITHUB_TOKEN` do not start further workflow runs, so `publish.yml` dispatches every follow-up workflow explicitly rather than relying on `release: published`. A new follow-up workflow needs both the dispatch step and its own `workflow_dispatch` inputs.
- A publish job must fail on a failed publish. A channel whose failure is demoted to a warning goes stale without anyone noticing.
- New release entrypoints must be added to the ownership table and covered by `tests/unit/test_release_entrypoint_docs.py`.
