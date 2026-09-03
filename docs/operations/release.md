# Release Operations

Bernstein follows a **two-track release cadence**:

- **Patch releases** (`vX.Y.Z`) are automated and ship approximately every 3 days when there are meaningful changes to ship. The `auto-release.yml` workflow tags releases automatically after CI passes on `main`.
- **Minor releases** (`vX.Y.0`) are manual, operator-initiated cuts that happen approximately every 2 weeks. The `release-major-minor.yml` workflow bumps the version and opens a PR that goes through the merge queue.

This ensures a predictable release schedule: patch fixes arrive every few days, while feature releases (minor and major) follow a planned milestone cadence.

This page documents which GitHub Actions workflows own each release entrypoint.

Update this table whenever a release workflow is added, renamed, or moved.

## Release Workflow Ownership

| Workflow | Name | Triggers | Owns | Handoff |
|---|---|---|---|---|
| `.github/workflows/post-ci-dispatcher.yml` | Post-CI dispatcher | `workflow_run` | Routes completed main-branch CI runs to release and recovery child workflows. | Calls `.github/workflows/auto-release.yml` when the upstream CI run targets `main`. |
| `.github/workflows/auto-release.yml` | Auto-release | `workflow_call` | Decides whether a green main-branch CI run should create a release tag. | Pushes a `v*` tag; `.github/workflows/publish.yml` owns tag publish. |
| `.github/workflows/publish.yml` | Publish | `push` | Builds release distributions, attests `dist/*`, publishes PyPI, npm and Copr RPM packages, and creates or updates the GitHub Release for a `v*` tag. | Dispatches the Docker, Homebrew, and SBOM follow-up workflows after creating the release, because a release created with `GITHUB_TOKEN` emits no `release: published` event. |
| `.github/workflows/release-major-minor.yml` | Major/Minor Release | `workflow_dispatch` | Manually cuts major or minor releases: checks CI, then bumps the version with `scripts/bump_version.py`. | Opens an `auto/bump-vX.Y.0` PR with auto-merge armed for the merge queue; `.github/workflows/auto-release.yml` tags the merge commit and `.github/workflows/publish.yml` builds, publishes, and creates the release from that tag -- the same chain patch releases use. |
| `.github/workflows/reconcile-release.yml` | Reconcile release drift | `schedule`, `workflow_dispatch` | Compares `pyproject.toml` against PyPI, Copr, npm, the Homebrew tap, and GitHub Release assets (dist + SBOM) to detect missed publish work. | Opens or updates a `release-drift` issue naming the channels that are behind; fails the run and leaves open issues open when a channel could not be read. |
| `.github/workflows/publish-docker.yml` | Publish Docker Image | `release`, `workflow_dispatch` | Publishes the GHCR image and image provenance for a released tag. | Dispatched by `publish.yml` after it creates the release; the `release` event covers releases created in the UI. |
| `.github/workflows/publish-homebrew.yml` | Publish Homebrew Formula | `release`, `workflow_dispatch` | Updates the Homebrew tap formula for a released version. | Dispatched by `publish.yml` after it creates the release; the `release` event covers releases created in the UI. |
| `.github/workflows/sbom.yml` | SBOM | `release`, `workflow_dispatch` | Generates SPDX + CycloneDX SBOMs and attaches them to the release that exists for the built ref. | Dispatched by `publish.yml` after it creates the release; the `release` event covers releases created in the UI. |

## Release Milestones

Bernstein uses GitHub milestones to plan minor and major releases. Patch releases do not get milestones; they ship from `main` as the `auto-release.yml` workflow determines they have meaningful changes.

Only open, version-numbered milestones are listed below -- a shipped version drops off this table rather than being marked done, and non-version tracking milestones (e.g. epics like "Volunteer workers") are out of scope since they don't follow this release cadence. Re-derive the rows from `gh api repos/sipyourdrink-ltd/bernstein/milestones` when this goes stale; don't hand-edit target dates.

| Milestone | Target Date | Status |
|---|---|---|
| `v3.20.0` | 2026-09-19 | Planned |
| `v4.0.0` | 2026-12-19 | Planned |

### Triage guidance

- **Small, high-priority issues** (bug fixes, minor improvements) target the **next imminent milestone** (e.g., v3.18.0 if we're two weeks before the date).
- **Larger work** (new features, refactorings, breaking changes) targets the **subsequent milestone** (e.g., v3.19.0 when v3.18.0 is already planned).
- **Major releases** (v4.0.0, etc.) are reserved for significant changes that justify a new major version. Open a discussion with maintainers before tagging major work for a major release milestone.

### Patch releases

Patch releases (`vX.Y.Z`) are automated by `auto-release.yml` and do **not** get milestones. They ship whenever:

- CI passes on `main` with a green `CI gate` check run.
- The triggering commit changes `pyproject.toml` version.
- At least one meaningful file changed since the last release (`src/`, `tests/`, `templates/`, `scripts/`, `packaging/`, `infra/`, `sdk/`, `services/`, `Dockerfile*`, `requirements*`, or `pyproject.toml`).

`auto-release.yml` decides per-run whether to tag; there is no manual intervention required. If the current version is already tagged, the workflow defers to the next bump PR opened via `release-major-minor.yml` or manual `scripts/bump_version.py` usage.

## Bumping the version

`scripts/bump_version.py` is the only supported way to bump the release version.

```
python scripts/bump_version.py 3.4.5
```

It performs the three coupled edits in one deterministic step:

1. rewrites `project.version` in `pyproject.toml`,
2. runs `uv lock` so `uv.lock` pins the new version, and
3. regenerates every distribution manifest via
   `scripts/gen_distribution_manifests.py`: `server.json`,
   `.plugin/plugin.json`, `plugin.json`, `mcp.json`, and `CITATION.cff`.

Do not hand-edit any of these files for a bump. A bump that touches only
`pyproject.toml` desyncs the lockfile and the distribution manifests, which the
CI drift gates then fail. Commit the bump in a PR; the merge to `main` triggers
CI and `.github/workflows/auto-release.yml` picks up the untagged version.

**The bump PR carries the generated manifests; there is no manual regeneration
step.** `release-major-minor.yml` restated a four-file subset in
`create-pull-request`'s `add-paths`, so `plugin.json` and `CITATION.cff` were
regenerated by the bump and then not committed, and every major/minor PR opened
with both a release behind (#4053). `Repo hygiene`'s drift check and
`test_repository_citation_cff_matches_the_package_version` both caught it, and
both were cleared by hand on the release branch. If you find yourself running
`gen_distribution_manifests.py` on a bump branch to make CI pass, that is the
bug returning, not the process.

The list the workflow commits is held to the generator's own
`GENERATED_MANIFESTS` by `tests/unit/test_release_bump_add_paths.py`, so a sixth
manifest cannot fall out of the bump the same way. Note that `.mcp.json` is an
*input* to that generator, not an output; it is read to project `mcp.json`.

## Release notes

Release history lives in `docs/release-notes/`, one `vX.Y.Z.md` page per tagged
version. The bump PR carries the page for the version it bumps to, and adds it
to the `Release notes` section of the `mkdocs.yml` nav. Entries that landed
since the newest tag are collected in `docs/release-notes/unreleased.md`; cutting
a version moves them into that version's page.

**Emptying `unreleased.md` is a step of the release PR, not a follow-up.** Move
every entry the tag ships onto the new version's page and delete it from
`unreleased.md` in the same commit; the page may legitimately end the pass
empty. `tests/unit/test_unreleased_notes_rotation.py` fails on any entry naming
an issue or PR a tagged release page already documents, so a page left
un-rotated stops the next PR rather than the next release.

The script does not write the page. `bernstein`'s local release-notes lookup
resolves the highest-versioned page in this directory, so a bump without its
page serves the previous release as if it were current;
`tests/unit/test_release_notes.py` fails on exactly that state.

`CHANGELOG.md` and `docs/CHANGELOG.md` are pointer documents. Do not add release
entries to them.

## RPM channel (Copr)

| Fact | Value |
|---|---|
| Copr project | <https://copr.fedorainfracloud.org/coprs/alexchernysh/bernstein/> |
| Repository secret | `COPR_CONFIG` |
| Publishing job | `publish-copr` in `.github/workflows/publish.yml` |
| Spec | `packaging/rpm/bernstein.spec` |
| Builder | `scripts/build_copr_srpm.py` |
| Build watcher | `scripts/copr_build_watch.py` |

`COPR_CONFIG` holds a complete `copr-cli` configuration file — the same
content `~/.config/copr` has on a workstation, including the `[copr-cli]`
header, `login`, `username`, `token` and `copr_url`. The job writes it
verbatim to `~/.config/copr` and restricts it to mode `600`; it is never
interpolated into shell text. The token carries an expiry date recorded as a
comment in the file itself, so it has to be reissued from the Copr web UI
before that date or the job starts failing.

The RPM ships the application and its full dependency closure in a private
virtualenv under `%{_libdir}/bernstein`, with `/usr/bin/bernstein` symlinked
to the venv's console script. `%install` resolves `bernstein==<release>` from
PyPI once, at RPM build time inside the Copr chroot; nothing resolves at run
time, so the installed command works without network access and always runs
the version the package metadata names. `%check` fails the build if the
packaged payload disagrees with that version. This is why the job waits for
the PyPI publish before submitting to Copr.

The package is arch-specific (not `noarch`): the closure carries compiled
extension modules, so each chroot builds its own payload against its own
interpreter ABI. On EPEL 9 the spec requires `python3.12` (parallel-installable
from AppStream) because the distribution's `python3` is 3.9, below the
project's floor; every other chroot uses `python3`.

Both version fields in the spec — `Version:` and `%global pypi_version` — are
bound to the release tag by the renderer at build time, so the committed
values are only what keeps the file buildable on its own. The two spellings
differ for pre-releases (`3.15.0~rc1` vs `3.15.0-rc1`).

Build a source RPM locally the same way the job does:

```
python3 scripts/build_copr_srpm.py --version v3.13.0 --outdir dist-rpm
python3 scripts/build_copr_srpm.py --version v3.13.0 --render-only   # no rpmbuild needed
```

Run the install smoke locally exactly the way CI runs it (needs Docker):

```
scripts/rpm_install_smoke.sh fedora:43 3.15.0
scripts/rpm_install_smoke.sh quay.io/centos/centos:stream9 3.15.0
```

### Chroots

The intended chroot set for the Copr project. This list is the single source
of truth: the smoke matrices in `ci.yml` (`install-smoke-rpm`) and
`publish.yml` (`rpm-install-smoke`) each cover one container per family named
here, and a chroot that cannot pass the smoke is removed from the project
rather than left publishing a package that does not install. Chroots are
enabled in the Copr project settings by the maintainer; nothing in CI mutates
the project.

| Chroot | Arches | Notes |
|---|---|---|
| `fedora-43` | x86_64, aarch64 | current stable |
| `fedora-44` | x86_64, aarch64 | current stable |
| `fedora-45` | x86_64, aarch64 | pre-release; non-gating until Python 3.15 wheels for cbor2/grpcio are available |
| `fedora-rawhide` | x86_64, aarch64 | follows Fedora branching, so a new Fedora release needs no manual edit here |
| `epel-9` | x86_64, aarch64 | spec requires `python3.12` from AppStream (distribution `python3` is 3.9) |
| `epel-10` | x86_64, aarch64 | distribution `python3` is 3.12 |

When Fedora branches a new release, Copr's rawhide chroot carries on and the
new stable chroot is enabled in the project settings; the smoke matrices pin
container tags and are updated in the same change.

### Republishing the RPM for an existing tag

The RPM channel builds from the spec rather than from `dist/`, so it can be
replayed on its own without cutting a release:

```
gh workflow run publish.yml --ref main -f tag=v3.13.0 -f copr_only=true
```

`copr_only` skips the release gates and the rest of the publish graph and runs
`publish-copr` alone. Submitting a version Copr already holds is rejected by
the build service, so bump the release or delete the existing build first.

### What fails the job, and what does not

| Outcome | Job result |
|---|---|
| Copr rejects the submission (bad SRPM, expired token) | fails |
| Copr accepts it but returns no build id | fails |
| The build ends `failed`, `canceled` or `skipped` within the watch window | fails |
| The build ends `succeeded` | passes |
| The build is still queued or running when the watch window expires | passes, with a `::notice::` naming the build URL |
| A pre-release chroot (`fedora-45`, `fedora-rawhide`) fails | passes with a `::notice::` naming the failed chroot(s); the release is not held |

The job has no warn-and-continue path for a *known-bad* outcome: nothing about
the RPM channel is visible from this repository, so a swallowed failure would
go unnoticed until someone installed the package by hand.

The last row is the one deliberate exception, and it is not a swallowed
failure — the submission already succeeded and the outcome is unknown rather
than bad. Copr's public builders are shared and its queue can run past any
sensible job budget; failing there would paint releases red for a build queue.
`reconcile-release.yml` compares the published Copr version daily and opens a
`release-drift` issue if the version never lands, which is exactly the case
this hands off to.

`copr-cli`'s own watch has no deadline — it runs until the build ends, however
long that takes — so the submission uses `--nowait` and
`scripts/copr_build_watch.py` owns the waiting against `COPR_WATCH_SECONDS`.
That budget must stay below the job's `timeout-minutes`, or a slow queue
cancels the step mid-wait instead of ending it; a guard test enforces the gap.

## Guardrails

- `.github/workflows/auto-release.yml` only creates tags.
- `.github/workflows/publish.yml` owns tag-triggered package and GitHub Release publication.
- `.github/workflows/reconcile-release.yml` is the drift detector for every published channel: PyPI, Copr, npm, the Homebrew tap, and the GitHub Release's dist and SBOM assets.
- Events raised with `GITHUB_TOKEN` do not start further workflow runs, so every entrypoint that creates a GitHub Release dispatches the follow-up workflows explicitly rather than relying on `release: published`. `publish.yml` is currently the only one -- `release-major-minor.yml` no longer creates a release itself, it opens a version-bump PR and lets `publish.yml` handle the rest. A new follow-up workflow needs a dispatch step there plus its own `workflow_dispatch` inputs; a new release entrypoint needs the full dispatch set.
- `release-major-minor.yml` decides readiness from **check runs**, not the combined status API. No lane in this repo posts a commit status, so `/commits/{sha}/status` answers `pending` with an empty `statuses` array on every commit here, green tags included. The gate reads `/commits/{sha}/check-runs`, requires `CI gate` (the sole required context) to be `success`, and refuses any other lane that ran to completion and failed. A cancelled lane is routine and does not block: queue-branch runs get cancelled to free runners.
- `release-major-minor.yml` does not run the suite; it defers to the matrix that already did. The step that used to sit after the readiness gate re-ran every test unsharded on the release job's single runner, against the same tree the gate had just proved green. It did not fit: at 49 minutes it was still running when the job's 60-minute `timeout-minutes` cancelled it, and a cancelled step cancels the release. The tree is covered twice without it -- `Test` runs the full OS/Python shard matrix on `main`, and the bump PR clears that same required matrix again in the merge queue. Do not add a test step back here; add coverage to `Test`, where it runs in parallel and gates every change rather than only releases.
- `release-major-minor.yml` never pushes directly to `main`. The branch ruleset has no bypass actors, so a direct push is rejected outright; the version bump lands through a pull request with auto-merge armed, same as every other change to `main` (#3948).
- A publish job must fail on a failed publish. A channel whose failure is demoted to a warning goes stale without anyone noticing.
- A check that could not run has not passed. `reconcile-release.yml` reports an unreadable channel as `unknown`, refuses to auto-close drift issues on a run carrying any `unknown`, and fails that run. The same rule applies to API lookups: only a 404 from the releases API means "no release exists", and every other failure fails the job instead of skipping the work it guards.
- New release entrypoints must be added to the ownership table and covered by `tests/unit/test_release_entrypoint_docs.py`.
- A lane that only runs at release time is a lane nothing has tested. `publish.yml`'s `Protocol Compatibility Gate` is the only caller of `tests/protocol/`, which sits outside both the default runner's `tests/unit` walk and the affected-test gate's `TEST_DIRS`. Its conftest applied a `protocol` marker that `[tool.pytest.ini_options] markers` never registered, and with `--strict-markers` that is an INTERNALERROR at collection, not a warning -- so the gate could only ever fail, and it first did so on a tag, after the freeze had lifted and before any channel had published. When a release-only lane is unavoidable, pin its preconditions from a test that ordinary CI does collect: `tests/unit/test_conftest_markers_are_registered.py` reads every conftest's `add_marker` names out of the source, so it fails on this class without needing the suite it protects to run.
