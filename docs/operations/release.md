# Release Operations

This page documents which GitHub Actions workflows own each release entrypoint.

Update this table whenever a release workflow is added, renamed, or moved.

## Release Workflow Ownership

| Workflow | Name | Triggers | Owns | Handoff |
|---|---|---|---|---|
| `.github/workflows/post-ci-dispatcher.yml` | Post-CI dispatcher | `workflow_run` | Routes completed main-branch CI runs to release and recovery child workflows. | Calls `.github/workflows/auto-release.yml` when the upstream CI run targets `main`. |
| `.github/workflows/auto-release.yml` | Auto-release | `workflow_call` | Decides whether a green main-branch CI run should create a release tag. | Pushes a `v*` tag; `.github/workflows/publish.yml` owns tag publish. |
| `.github/workflows/publish.yml` | Publish | `push` | Builds release distributions, attests `dist/*`, publishes PyPI and npm packages, and creates or updates the GitHub Release for a `v*` tag. | GitHub Release publication triggers Docker, Homebrew, and release-scoped SBOM follow-up workflows. |
| `.github/workflows/release-major-minor.yml` | Major/Minor Release | `workflow_dispatch` | Manually cuts major or minor releases after checking CI and applying the version bump. | Pushes the version commit and tag, then builds and publishes from the same run. |
| `.github/workflows/release-please.yml` | Release Please | `workflow_dispatch` | Maintains release PRs and release metadata when an operator runs it manually. | Does not publish artifacts; publish still starts from a `v*` tag or a manual major/minor run. |
| `.github/workflows/reconcile-release.yml` | Reconcile release drift | `schedule`, `workflow_dispatch` | Compares `pyproject.toml`, PyPI, and GitHub Release assets to detect missed publish work. | Opens or updates a `release-drift` issue when published state is inconsistent. |
| `.github/workflows/publish-docker.yml` | Publish Docker Image | `release`, `workflow_dispatch` | Publishes the GHCR image and image provenance for a released tag. | Runs after a GitHub Release is published, or manually for a selected tag. |
| `.github/workflows/publish-homebrew.yml` | Publish Homebrew Formula | `release`, `workflow_dispatch` | Updates the Homebrew tap formula for a released version. | Runs after a GitHub Release is published, or manually for a selected version. |
| `.github/workflows/sbom-upload.yml` | SBOM upload | `push`, `release` | Generates and uploads the CycloneDX SBOM when the Dependency-Track endpoint is configured. | Runs on main updates and after a GitHub Release is published. |

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
- `.github/workflows/reconcile-release.yml` is the drift detector for missing PyPI versions or empty GitHub Release assets.
- New release entrypoints must be added to the ownership table and covered by `tests/unit/test_release_entrypoint_docs.py`.
