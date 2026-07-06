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

## 2. Install CodeRabbit GitHub App

Free Pro tier for OSS repos. URL: <https://github.com/apps/coderabbitai>.

Steps:
- Click **Install** → authorize on `sipyourdrink-ltd/bernstein`.
- No repo secret required.
- Tuned `.coderabbit.yaml` lives at the repo root (path-aware instructions,
  Pro features enabled, duplicate-CI tools disabled).
- Companion `.sourcery.yaml` lives alongside it; the Sourcery CLI runs as an
  advisory PR lane in `.github/workflows/code-review-bots-ci.yml`.
- Secrets required: `CODERABBIT_API_KEY` (chat-only, optional) and
  `SOURCERY_API_KEY` (used by the CLI lane).

Risk: adds 1 reviewer comment per PR. Rate-limit is 4 reviews/hr/PR; bursty
force-pushes will queue.

---

## 3. Install Gemini Code Assist GitHub App

Free tier: 240 review sessions/day (2026). Install the Gemini Code Assist app from the GitHub Marketplace.

Steps:
- **Install** → authorize on `sipyourdrink-ltd/bernstein`.
- Auth flows through the maintainer's Google account; no repo secret needed.

Risk: doubles AI-reviewer noise alongside CodeRabbit. Worth keeping for
cross-check on security-sensitive PRs; consider disabling per-PR if signal/noise
degrades.

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

## 10. COPR / RPM - kill or fix decision

**Status:** ❌ broken since March 2026. Last successful build was `1.4.11`.
Every build since fails in Fedora chroots - `copr-cli buildpypi` cannot
resolve 30+ Python `python3dist(...)` dependencies (`beartype >= 0.21`,
`crosshair-tool`, `openai-agents`, etc.) because they are not packaged for
Fedora.

The wrapper-spec workaround landed in `packaging/rpm/bernstein.spec`
(`1.4.11-1`) but it is unused: `publish.yml` calls `copr-cli buildpypi`,
which **ignores** the in-repo spec and synthesizes its own from PyPI
metadata. That regenerated spec is what pulls in the missing
`python3dist(...)` BuildRequires.

The operator has to pick one of two paths. Both are docs-only here - the
actual workflow / docs edits land in a follow-up PR.

### Option A - Kill the channel

Lowest-effort, recommended if COPR install volume is < 5% of downloads.

Diff for `.github/workflows/publish.yml`:

```diff
-  # COPR RPM rebuild - triggers a new build from the updated PyPI release.
-  trigger-copr:
-    name: Trigger COPR rebuild
-    runs-on: ubuntu-latest
-    needs: build
-    timeout-minutes: 10
-    permissions: {}
-    steps:
-      - name: Harden runner (audit mode)
-        uses: step-security/harden-runner@ab7a9404c0f3da075243ca237b5fac12c98deaa5 # v2.19.3
-        with:
-          egress-policy: audit
-      - name: Trigger COPR build
-        run: |
-          pip install copr-cli
-          mkdir -p ~/.config
-          cat > ~/.config/copr << EOF
-          [copr-cli]
-          login = ${{ secrets.COPR_LOGIN }}
-          username = alexchernysh
-          token = ${{ secrets.COPR_TOKEN }}
-          copr_url = https://copr.fedorainfracloud.org
-          EOF
-          copr-cli buildpypi --packagename bernstein alexchernysh/bernstein --nowait
```

Then strip the COPR install path from:

- `docs/getting-started/install.md` (the `Fedora / RHEL (dnf)` tab)
- `docs/getting-started/install-linux.md` (`## Fedora / RHEL via COPR`)
- `docs/index.html` (FAQ schema mentions `dnf copr`)
- `docs/llms-full.txt`
- All `docs/i18n/README.*.md` install matrix rows
- `packaging/rpm/bernstein.spec` (delete the file; no longer published)
- Repo secrets: delete `COPR_LOGIN` and `COPR_TOKEN`.

Recommend a redirect notice: point Fedora users at `pipx install bernstein`
or `uv tool install bernstein` - both work on Fedora 41/42 out of the box.

### Option B - Fix the spec

Keep COPR alive. Stop relying on `buildpypi`'s auto-generated spec; ship the
in-repo spec and let `%pyproject_buildrequires --generate-extras` discover
Python deps from `pyproject.toml` at build time, falling back to bundled
wheels for anything Fedora can't resolve.

Diff for `packaging/rpm/bernstein.spec` (replaces the entire current file):

```diff
-Name:           bernstein
-Version:        1.4.11
-Release:        1%{?dist}
-Summary:        Multi-agent orchestration for AI coding agents
-License:        Apache-2.0
-URL:            https://github.com/sipyourdrink-ltd/bernstein
-BuildArch:      noarch
-Requires:       python3 >= 3.12
-
-%description
-Orchestrate parallel AI coding agents. Runs Claude Code, Codex, Gemini CLI
-and others in parallel with git worktree isolation and quality gates.
-
-%install
-mkdir -p %{buildroot}%{_bindir}
-cat > %{buildroot}%{_bindir}/bernstein << 'WRAPPER'
-#!/bin/bash
-if command -v pipx &>/dev/null; then
-    exec pipx run bernstein "$@"
-elif command -v uvx &>/dev/null; then
-    exec uvx bernstein "$@"
-else
-    exec python3 -m pip install --user bernstein &>/dev/null && exec python3 -m bernstein "$@"
-fi
-WRAPPER
-chmod 755 %{buildroot}%{_bindir}/bernstein
-
-%files
-%{_bindir}/bernstein
+%global pypi_name bernstein
+
+Name:           bernstein
+Version:        2.0.1
+Release:        1%{?dist}
+Summary:        Multi-agent orchestration for AI coding agents
+License:        Apache-2.0
+URL:            https://github.com/sipyourdrink-ltd/bernstein
+Source0:        %{pypi_source %{pypi_name}}
+BuildArch:      noarch
+
+BuildRequires:  python3-devel >= 3.12
+BuildRequires:  pyproject-rpm-macros
+
+%description
+Orchestrate parallel AI coding agents. Runs Claude Code, Codex, Gemini CLI
+and others in parallel with git worktree isolation and quality gates.
+
+%prep
+%autosetup -n %{pypi_name}-%{version}
+
+%generate_buildrequires
+# --generate-extras lets pyproject-rpm-macros discover optional deps from
+# pyproject.toml so missing python3dist(...) Fedora packages don't block
+# the build. Anything not available in Fedora is pulled from bundled wheels
+# via %pyproject_wheel.
+%pyproject_buildrequires -r --generate-extras
+
+%build
+%pyproject_wheel
+
+%install
+%pyproject_install
+%pyproject_save_files %{pypi_name}
+
+%files -f %{pypi_name}.files
+%license LICENSE
+%doc README.md
+%{_bindir}/bernstein
```

Then change `publish.yml` to upload the in-repo spec instead of using
`buildpypi`:

```diff
-          copr-cli buildpypi --packagename bernstein alexchernysh/bernstein --nowait
+          copr-cli build --nowait alexchernysh/bernstein packaging/rpm/bernstein.spec
```

Expect 2–3 iterations: each rebuild reveals the next missing
`python3dist(...)` that needs to either land in Fedora or get vendored via a
`Source1:` wheel bundle.

### Recommendation

**Option A (kill).** Reasoning:

- `pipx`/`uv tool install` covers Fedora natively and is the upstream
  Python recommendation; COPR is duplicate surface.
- Option B's "2–3 iterations" is optimistic - `crosshair-tool` and
  `openai-agents` have transitive deps that Fedora has historically taken
  6+ months to package. Realistic timeline is months of maintenance for
  marginal install volume.
- Killing COPR also frees the operator from rotating `COPR_LOGIN` /
  `COPR_TOKEN` and from monitoring a chronically red build.

Pick Option B only if a downstream consumer (gov / regulated org) has a
hard requirement for a signed `.rpm` from a Fedora-trusted source. That
requirement should be documented before reopening the channel.
