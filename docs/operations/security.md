# Security operations

Operator-facing runbook for security posture signals. See also
[security-and-identity.md](security-and-identity.md) for the runtime
security and identity stack.

## OSSF Scorecard

Weekly Scorecard runs via `.github/workflows/scorecard.yml`. Results are
uploaded to GitHub Code Scanning. Per-signal triage decisions live in
issue #1482.

## CII Best Practices badge (operator pickup)

The CII Best Practices badge cannot be obtained programmatically. An
operator with a verifiable email address must register the project.

### Registration checklist

1. Sign in at https://www.bestpractices.dev with a GitHub or email
   identity owned by a maintainer.
2. Add a new project. Fields:
   - **Repository URL:** `https://github.com/sipyourdrink-ltd/bernstein`
   - **Project home page:** `https://bernstein.run`
   - **License:** Apache-2.0 (see `LICENSE`)
   - **Primary language:** Python
3. Complete the passing-level self-assessment questionnaire. The
   cross-walk below maps each criterion to existing controls so the
   answers are mechanical.
4. Copy the assigned project ID (a numeric string) from the project
   page URL.
5. Add the bestpractices.dev badge with the assigned project ID to
   `README.md` (alongside the existing badges at the top of the file).

### Self-assessment cross-walk

Mapping of the Passing-tier criteria most reviewers ask about to
existing artefacts in this repository.

| Criterion | Where it is satisfied |
|-----------|------------------------|
| Public source code repository with version control | `https://github.com/sipyourdrink-ltd/bernstein` (git, public). |
| Project website and discussion channel | `https://bernstein.run`, GitHub Issues, GitHub Discussions. |
| Documented contribution process | `CONTRIBUTING.md`. |
| OSI-approved license, license file present | `LICENSE` (Apache-2.0). |
| Documented build instructions | `README.md` "install" section; `docs/getting-started/install.md`. |
| Cryptographically signed releases | `release-please` + signed PyPI uploads; `SECURITY.md`. |
| Vulnerability reporting process | `SECURITY.md`. |
| Documented secure development knowledge for at least one committer | `docs/operations/security-and-identity.md`; CONTRIBUTING checklist. |
| Public bug tracker | GitHub Issues. |
| Test suite invocable with a documented command | `pytest`; `README.md` "Build & test" block. |
| Static analysis (SAST) in CI | CodeQL (`.github/workflows/codeql.yml`); Bandit and Semgrep jobs in `.github/workflows/ci.yml`; `.github/workflows/static-analysis-extended.yml`. |
| Dynamic analysis / fuzzing | Hypothesis property tests in `tests/`; ClusterFuzzLite at `.clusterfuzzlite/` + `.github/workflows/cifuzz-pr.yml`. |
| Dependency vulnerability scanning | Dependabot, OSV via Scorecard, `.github/workflows/dependency-review.yml`. |
| Cryptographic primitives are well-known | HMAC-SHA256, Ed25519/EdDSA, JWS detached. See module map in `CLAUDE.md` under `src/bernstein/core/security/`. |

### Re-evaluation cadence

Re-run Scorecard after the badge lands in `README.md` to confirm the
CIIBestPracticesID signal flips from 0 to a positive score.

## Fuzzing harness (OSSF-recognized)

ClusterFuzzLite gives OSSF Scorecard a signal it recognizes. Files:

- `.clusterfuzzlite/project.yaml` -- language / engine / sanitizer.
- `.clusterfuzzlite/Dockerfile` -- builder image (pinned by digest).
- `.clusterfuzzlite/build.sh` -- atheris harness compilation. Installs
  PyYAML via `pip3 install --require-hashes -r requirements.txt` to
  satisfy Scorecard `PinnedDependenciesID`.
- `.clusterfuzzlite/requirements.txt` -- hash-pinned PyYAML for the
  build step.
- `.clusterfuzzlite/fuzz_seed_parser.py` -- minimal entry point against
  `yaml.safe_load`, the parser primitive `bernstein.core.config.seed_parser`
  sits on top of (the OSS-Fuzz base-builder-python image ships Python
  3.11; bernstein requires 3.12+, so the harness targets the underlying
  YAML primitive instead of importing the full package).
- `.github/workflows/cifuzz-pr.yml` -- per-PR run via
  `google/clusterfuzzlite/actions/run_fuzzers` (SHA-pinned). Job-level
  token permissions are kept read-only (Scorecard `TokenPermissionsID`);
  SARIF upload is intentionally disabled so the elevated
  `security-events: write` scope is not needed. Crash artefacts are
  surfaced via `actions/upload-artifact`.

The Hypothesis property-test suite remains the primary fuzzing surface
for real bug detection. ClusterFuzzLite is signal-only here -- it
exists so Scorecard's `Fuzzing` check finds something it understands.

## MaintainedID 90-day self-resolve

Scorecard's `MaintainedID` returns 0 for any repo younger than 90 days.
The repository has now passed the 90-day threshold, so the signal has
self-resolved. Issue #1482 tracked the checkpoint and is closed; the
one-shot re-check workflow that watched for the threshold has been
removed now that it has served its purpose.
