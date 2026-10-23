# CI apps & integrations - one-time operator playbook

Forward-looking install guide for free OSS-tier GitHub Apps and platform features
that benefit the `sipyourdrink-ltd/bernstein` repo. Each section is a single
operator action: click, authorize, done. Apply in any order; nothing here is a
blocker for day-to-day development.

Tracking issue: [#1273](https://github.com/sipyourdrink-ltd/bernstein/issues/1273).

---

## 1. Enable CodeQL "default setup"

Result: GitHub-hosted CodeQL scanning + Copilot Autofix suggestions on
code-scanning alerts. Zero workflow YAML to maintain.

Current state: the repo already runs CodeQL through the advanced workflow
`.github/workflows/codeql.yml` (Python, on push to `main` and weekly).
GitHub does not allow default setup alongside an advanced workflow, so this
step applies only if that workflow is retired.

Steps:
- GitHub repo → **Settings** → **Code security** → **Code scanning** → **Set up** → **Default**.
- Pick the languages GitHub detects (Python is auto-suggested).
- Confirm.

Risk: CodeQL produces some false positives on first scan. Autofix proposes
patches as PR suggestions - it never auto-merges. Triage as normal review work.

---

## 2. CodeRabbit and Sourcery - retired

Both apps are retired and no longer in use on this repository. Their repo
configuration is gone: `.coderabbit.yaml`, `.sourcery.yaml`, and the advisory
CLI lane `.github/workflows/code-review-bots-ci.yml` were removed.

Remaining operator actions (owner-only, browser):

- Org **Settings** → **GitHub Apps** → uninstall **CodeRabbit** and
  **Sourcery** if still installed.
- Delete the stale repo secrets `CODERABBIT_API_KEY` and `SOURCERY_API_KEY`
  (**Settings** → **Secrets and variables** → **Actions**); nothing reads
  them any more.

---

## 3. Install Gemini Code Assist GitHub App

Free tier: 240 review sessions/day (2026). Install the Gemini Code Assist app from the GitHub Marketplace.

Steps:
- **Install** → authorize on `sipyourdrink-ltd/bernstein`.
- Auth flows through the maintainer's Google account; no repo secret needed.

Risk: adds a second AI-reviewer lane next to the one already reviewing PRs.
Worth keeping for cross-check on security-sensitive PRs; consider disabling
per-PR if signal/noise degrades.

---

## 4. Enable GitHub Actions Insights tab

Free, no install. Path: **Repo → Insights → Actions**.

Use as a 30-day "main CI green/red" gauge and per-workflow runtime trend. No
configuration needed - the tab populates from existing workflow runs.

---

## 5. Configure PyPI Trusted Publishing (OIDC)

Replaces the long-lived `PYPI_API_TOKEN` secret with short-lived OIDC tokens
minted per release run.

Steps:
- Visit <https://pypi.org/manage/account/publishing/>.
- Add a publisher: PyPI project `bernstein` → workflow `publish.yml`
  → environment `pypi`.
- After the next successful release run confirms OIDC works, delete the
  `PYPI_API_TOKEN` repo secret (no workflow under `.github/workflows/`
  reads it any more).

Risk: first-time setup requires an existing PyPI account that owns the
`bernstein` project. Keep the API token around until one OIDC release succeeds.

---

## 6. GitHub merge queue - done

The queue is active on `main` through the repository ruleset
`main-merge-queue`, not branch protection. The required workflows trigger
on `merge_group`. The queue's required contexts are `CI gate` and
`shipped bundle matches the lockfile`. Configuration and tunables:
[docs/operations/merge-queue.md](../operations/merge-queue.md).

---

## 7. (Optional) StepSecurity public dashboard

URL: <https://app.stepsecurity.io>.

Steps:
- Sign in with GitHub → grant read access.
- `bernstein` appears in the dashboard automatically.

Result: egress baseline review and policy suggestions, visible as runs
collect data from the `harden-runner` audit-mode step the workflows already
carry.

Risk: external UI; the egress data stays publicly visible.

---

## 8. (Optional) Renovate vs Dependabot evaluation

Both are configured: `.github/dependabot.yml` (weekly, grouped; patch/minor
groups are auto-enqueued by `dependabot-auto-merge.yml`) and `renovate.json`
(custom managers and package rules). No action required now; if the two
produce duplicate PRs, retire one.

---

## 9. Homebrew tap - wire up `HOMEBREW_TAP_TOKEN`

**Status:** the tap is only updated when `HOMEBREW_TAP_TOKEN` is defined on
the `release-channels` environment. `publish-homebrew.yml` runs on every
release; a preflight step fails the run early if the secret is missing there,
and the "Push to homebrew-tap repo" step refuses to push anonymously. There is
no `continue-on-error` and no `GITHUB_TOKEN` fallback.

### Why it must be an environment secret

The job runs under `environment: release-channels`, and an environment secret
does not follow a job across environments: a value defined on `pypi` (or as a
plain repo secret) is not visible to it. `GITHUB_TOKEN` only scopes to the
current repo, so cloning and pushing to `chernistry/homebrew-tap` needs a PAT.

### What the operator needs to do (one sitting)

| # | Action | Where |
|---|--------|-------|
| 1 | Generate a PAT with write access to `chernistry/homebrew-tap` only (fine-grained: **Contents: Read & write**). 90-day expiry. | <https://github.com/settings/personal-access-tokens/new> |
| 2 | Add the PAT as secret `HOMEBREW_TAP_TOKEN` on the `release-channels` environment. | <https://github.com/sipyourdrink-ltd/bernstein/settings/environments> |
| 3 | Re-dispatch `publish-homebrew.yml` for the current release. | <https://github.com/sipyourdrink-ltd/bernstein/actions/workflows/publish-homebrew.yml> |
| 4 | Verify the tap commit landed. | <https://github.com/chernistry/homebrew-tap/commits/main> |

### Commands

PAT generation is browser-only (GitHub does not expose fine-grained PAT
creation via API). After the PAT exists, the rest can run from a terminal
authenticated with `gh auth login`:

```sh
# 2. Add the PAT as an environment secret (paste PAT at the prompt)
gh secret set HOMEBREW_TAP_TOKEN \
  --env release-channels \
  --repo sipyourdrink-ltd/bernstein

# 3. Re-dispatch the workflow against the current release version
gh workflow run publish-homebrew.yml \
  --repo sipyourdrink-ltd/bernstein \
  --ref main \
  -f version=<current release version>

# 4. Wait + check the run
gh run watch --repo sipyourdrink-ltd/bernstein

# 5. Confirm the tap got the bump
gh api repos/chernistry/homebrew-tap/contents/Formula/bernstein.rb \
  --jq '.content' | base64 -d | grep -E '^\s*url|^\s*sha256'
```

### Risk

- PAT scope is repo-narrow and Contents-only - minimum needed for `git push`
  to `homebrew-tap`. Don't broaden it.
- 90-day rotation reminder: add to the operator's calendar; an expired PAT
  still passes the preflight (which only checks that the secret is defined)
  and makes the next release's homebrew run fail at the "Push to
  homebrew-tap repo" step with "homebrew-tap repo not reachable".

---

## 10. COPR / RPM - resolved

**Status:** ✅ wired into the release chain (#3325).

The channel was broken from March 2026 to August 2026 because the release
chain called `copr-cli buildpypi`, which ignores the in-repo spec and
synthesizes its own from PyPI metadata. That generated spec pulls in 30+
`python3dist(...)` BuildRequires that Fedora does not package, so every
chroot build failed. The last successful build was `1.4.11`.

The fix keeps the channel and drops `buildpypi`. The first replacement spec
shipped a launcher that resolved the package through `pipx`/`uvx` at run time;
that made the RPM version describe nothing and the package unusable offline
(#3558), so `packaging/rpm/bernstein.spec` now installs the release and its
dependency closure into a private virtualenv at RPM build time. Nothing has to
be packaged for Fedora - the closure comes from the released wheels - and
nothing resolves at run time. `scripts/build_copr_srpm.py` binds the spec to
the release tag, `rpm-install-smoke` installs the built RPM per chroot family
and runs it with networking disabled, and `publish-copr` in
`.github/workflows/publish.yml` submits the SRPM only after that smoke passes
(#3559).

Operator details - secret name, project URL, local build commands, and the
single-channel republish path - live in
[`docs/operations/release.md`](../operations/release.md#rpm-channel-copr).
The channel is in the `reconcile-release.yml` comparison set, so a version the
RPM channel never received opens a `release-drift` issue.
