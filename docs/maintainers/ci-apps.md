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
CLI lane `.github/workflows/code-review-bots-ci.yml` were removed, and the
`review-bot-ack` gate no longer lists either account in `REVIEW_BOT_LOGINS`
(`scripts/review_bot_ack.py`).

Remaining operator actions (owner-only, browser):

- Org **Settings** → **GitHub Apps** → uninstall **CodeRabbit** and
  **Sourcery** if still installed.
- Delete the stale repo secrets `CODERABBIT_API_KEY` and `SOURCERY_API_KEY`
  (**Settings** → **Secrets and variables** → **Actions**); nothing reads
  them any more.

The acknowledgement gate itself stays: it tracks whichever reviewer accounts
`REVIEW_BOT_LOGINS` names (currently `baz-reviewer[bot]`). See
`docs/operations/review-bot-ack.md` for the protocol.

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
- Add a publisher: PyPI project `bernstein` → workflow `auto-release.yml`
  (or whichever workflow publishes) → environment `pypi`.
- After the next successful release run confirms OIDC works, delete the
  `PYPI_API_TOKEN` repo secret.

Risk: first-time setup requires an existing PyPI account that owns the
`bernstein` project. Keep the API token around until one OIDC release succeeds.

---

## 6. Enable GitHub merge queue

Free for org-owned public repos in 2026.

Steps: **Repo → Settings → Branches** → edit `main` branch protection rule →
enable **Merge queue**.

Caveats:
- Pair with `required_status_checks.strict: false` - merge queue is
  incompatible with "require branches to be up to date".
- Required workflows must trigger on `merge_group`:
  `on: merge_group: types: [checks_requested]`.
- Verify after [#1277](https://github.com/sipyourdrink-ltd/bernstein/pull/1277)
  lands - that PR adds the `merge_group` trigger to required workflows.

---

## 7. (Optional) StepSecurity public dashboard

URL: <https://app.stepsecurity.io>.

Steps:
- Sign in with GitHub → grant read access.
- `bernstein` appears in the dashboard automatically.

Result: egress baseline review and policy suggestions, visible once the
`harden-runner` audit mode from PR HD-6 lands and runs collect data.

Risk: external UI; the egress data stays publicly visible.

---

## 8. (Optional) Renovate vs Dependabot evaluation

Not yet. Dependabot stays the primary dependency-update bot today.

Re-evaluate in ~1 quarter against Renovate's group/dashboard features if
Dependabot PR noise becomes a problem. No action required now.

---

## 9. Homebrew tap - wire up `HOMEBREW_TAP_TOKEN`

**Status:** ⚠️ tap stuck at `1.4.1`. `publish-homebrew.yml` runs on every
release but the "Push to homebrew-tap repo" step silently no-ops because the
`HOMEBREW_TAP_TOKEN` secret is missing. The step is guarded by
`continue-on-error: true`, so the workflow is green while the tap drifts.

### Why it's silent

`.github/workflows/publish-homebrew.yml` (line 88):

```yaml
GH_TOKEN: ${{ secrets.HOMEBREW_TAP_TOKEN || secrets.GITHUB_TOKEN }}
```

`GITHUB_TOKEN` only scopes to the current repo, so `gh repo clone
chernistry/homebrew-tap` and `git push` to that external repo cannot succeed
without a PAT. The step prints a `::warning::` and exits 0.

### What the operator needs to do (one sitting)

| # | Action | Where |
|---|--------|-------|
| 1 | Generate fine-grained PAT, **Contents: Read & write** scope on `chernistry/homebrew-tap` only. 90-day expiry. | <https://github.com/settings/personal-access-tokens/new> |
| 2 | Add the PAT as repo secret `HOMEBREW_TAP_TOKEN`. | <https://github.com/sipyourdrink-ltd/bernstein/settings/secrets/actions/new> |
| 3 | Re-dispatch `publish-homebrew.yml` for the current release (`v2.0.1`). | <https://github.com/sipyourdrink-ltd/bernstein/actions/workflows/publish-homebrew.yml> |
| 4 | Verify the tap commit landed. | <https://github.com/chernistry/homebrew-tap/commits/main> |

### Commands

PAT generation is browser-only (GitHub does not expose fine-grained PAT
creation via API). After the PAT exists, the rest can run from a terminal
authenticated with `gh auth login`:

```sh
# 2. Add the PAT as repo secret (paste PAT at the prompt)
gh secret set HOMEBREW_TAP_TOKEN \
  --repo sipyourdrink-ltd/bernstein \
  --app actions

# 3. Re-dispatch the workflow against the current release tag
gh workflow run publish-homebrew.yml \
  --repo sipyourdrink-ltd/bernstein \
  --ref main \
  -f version=2.0.1

# 4. Wait + check the run
gh run watch --repo sipyourdrink-ltd/bernstein

# 5. Confirm the tap got the bump
gh api repos/chernistry/homebrew-tap/contents/Formula/bernstein.rb \
  --jq '.content' | base64 -d | grep -E '^\s*url|^\s*sha256'
```

### Risk

- PAT scope is repo-narrow and Contents-only - minimum needed for `git push`
  to `homebrew-tap`. Don't broaden it.
- 90-day rotation reminder: add to the operator's calendar; expired PAT
  silently regresses to the same broken state.
- After the first successful re-dispatch, follow-up in a separate PR:
  flip `continue-on-error: true` to `false` on the "Push to homebrew-tap
  repo" step so future regressions surface immediately.

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
