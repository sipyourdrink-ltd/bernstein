# Release Operations

This page documents which GitHub Actions workflows own each release entrypoint.

Update this table whenever a release workflow is added, renamed, or moved.

## Release Workflow Ownership

| Workflow | Name | Triggers | Owns | Handoff |
|---|---|---|---|---|
| `.github/workflows/post-ci-dispatcher.yml` | Post-CI dispatcher | `workflow_run` | Routes completed main-branch CI runs to release and recovery child workflows. | Calls `.github/workflows/auto-release.yml` when the upstream CI run targets `main`. |
| `.github/workflows/auto-release.yml` | Auto-release | `workflow_call` | Decides whether a green main-branch CI run should create a release tag. | Pushes a `v*` tag; `.github/workflows/publish.yml` owns tag publish. |
| `.github/workflows/publish.yml` | Publish | `push` | Builds release distributions, attests `dist/*`, publishes PyPI, npm and Copr RPM packages, and creates or updates the GitHub Release for a `v*` tag. | GitHub Release publication triggers Docker, Homebrew, and release-scoped SBOM follow-up workflows. |
| `.github/workflows/release-major-minor.yml` | Major/Minor Release | `workflow_dispatch` | Manually cuts major or minor releases after checking CI and applying the version bump. | Pushes the version commit and tag, then builds and publishes from the same run. |
| `.github/workflows/reconcile-release.yml` | Reconcile release drift | `schedule`, `workflow_dispatch` | Compares `pyproject.toml`, PyPI, Copr, and GitHub Release assets to detect missed publish work. | Opens or updates a `release-drift` issue when published state is inconsistent. |
| `.github/workflows/publish-docker.yml` | Publish Docker Image | `release`, `workflow_dispatch` | Publishes the GHCR image and image provenance for a released tag. | Runs after a GitHub Release is published, or manually for a selected tag. |
| `.github/workflows/publish-homebrew.yml` | Publish Homebrew Formula | `release`, `workflow_dispatch` | Updates the Homebrew tap formula for a released version. | Runs after a GitHub Release is published, or manually for a selected version. |
| `.github/workflows/sbom.yml` | SBOM | `release`, `workflow_dispatch` | Generates SPDX + CycloneDX SBOMs and attaches them as release assets. | Runs after a GitHub Release is published, or manually for a selected ref. |

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

## RPM channel (Copr)

| Fact | Value |
|---|---|
| Copr project | <https://copr.fedorainfracloud.org/coprs/alexchernysh/bernstein/> |
| Repository secret | `COPR_CONFIG` |
| Publishing job | `publish-copr` in `.github/workflows/publish.yml` |
| Spec | `packaging/rpm/bernstein.spec` |
| Builder | `scripts/build_copr_srpm.py` |

`COPR_CONFIG` holds a complete `copr-cli` configuration file — the same
content `~/.config/copr` has on a workstation, including the `[copr-cli]`
header, `login`, `username`, `token` and `copr_url`. The job writes it
verbatim to `~/.config/copr` and restricts it to mode `600`; it is never
interpolated into shell text. The token carries an expiry date recorded as a
comment in the file itself, so it has to be reissued from the Copr web UI
before that date or the job starts failing.

The RPM is a wrapper: it installs a `bernstein` launcher that resolves the
real package through `pipx`/`uvx` at run time, which is why the job waits for
the PyPI publish and why the spec declares no Python dependencies. The version
is bound to the release tag at build time, so the `Version:` committed in the
spec is only what keeps the file buildable on its own.

Build a source RPM locally the same way the job does:

```
python3 scripts/build_copr_srpm.py --version v3.13.0 --outdir dist-rpm
python3 scripts/build_copr_srpm.py --version v3.13.0 --render-only   # no rpmbuild needed
```

### Republishing the RPM for an existing tag

The RPM channel builds from the spec rather than from `dist/`, so it can be
replayed on its own without cutting a release:

```
gh workflow run publish.yml --ref main -f tag=v3.13.0 -f copr_only=true
```

`copr_only` skips the release gates and the rest of the publish graph and runs
`publish-copr` alone. Submitting a version Copr already holds is rejected by
the build service, so bump the release or delete the existing build first.

The job waits for the Copr build instead of returning at upload, and it has no
warn-and-continue path: a rejected submission or a failed chroot build fails
the job. Nothing about the RPM channel is visible from this repository, so a
swallowed failure would go unnoticed until someone installed the package by
hand.

## Guardrails

- `.github/workflows/auto-release.yml` only creates tags.
- `.github/workflows/publish.yml` owns tag-triggered package and GitHub Release publication.
- `.github/workflows/reconcile-release.yml` is the drift detector for missing PyPI versions, a stale Copr RPM, or empty GitHub Release assets.
- New release entrypoints must be added to the ownership table and covered by `tests/unit/test_release_entrypoint_docs.py`.
