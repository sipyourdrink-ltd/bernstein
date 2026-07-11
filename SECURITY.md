# Security Policy

> Last reviewed: 2026-05-18.

## Reporting a Vulnerability

**Do not open a public issue for security vulnerabilities.**

### Preferred channel - Email

**forte@bernstein.run** (PGP key: `/.well-known/security-pgp.asc`)

Reports are triaged within 72 hours.

### Alternative - GitHub private security advisory

Open a private advisory so triage and the fix stay in one place:

**https://github.com/sipyourdrink-ltd/bernstein/security/advisories/new**

---

## Bug Bounty Program

### Scope

#### In scope

| Target | Notes |
|--------|-------|
| `github.com/sipyourdrink-ltd/bernstein` - Python package | `src/bernstein/` |
| Task server API (`localhost:8052` when self-hosted) | All HTTP endpoints |
| Agent spawner / orchestrator | Privilege escalation, task injection |
| Docker images (`bernstein:latest`, `bernstein:sandbox`) | Container escapes |
| CLI entry points (`bernstein run`, `bernstein server`, etc.) | Argument injection |
| Authentication tokens (agent tokens, `BERNSTEIN_AUTH_TOKEN`) | Token forgery, replay |

#### Out of scope

- Third-party CLI agents (Claude Code, Codex, Gemini CLI) - report to their vendors
- The researcher's own sandboxed instance if improperly configured
- Social engineering / phishing attacks
- Denial-of-service against the public demo (rate-limit the test, not the server)
- Vulnerabilities in dependencies where no Bernstein-specific exploit path exists
- Reports that require physical access to the machine

### Severity and Rewards

| Severity | CVSS | Examples | Reward range |
|----------|------|---------|--------------|
| Critical | 9.0–10.0 | RCE on task server, container escape, token forgery enabling full takeover | $1 000 – $5 000 |
| High | 7.0–8.9 | Privilege escalation, unauthenticated task injection, path traversal outside workspace | $250 – $1 000 |
| Medium | 4.0–6.9 | Auth bypass for low-privilege endpoints, info disclosure of agent tokens, SSRF | $100 – $250 |
| Low | 0.1–3.9 | Minor info disclosure, non-exploitable misconfigurations | $25 – $100 |

Rewards are paid in USD. Minimum payout threshold: $25.

Duplicate reports receive no reward. First valid reporter wins.

### Response SLAs

| Milestone | Target |
|-----------|--------|
| Initial triage acknowledgement | ≤ 72 hours |
| Severity confirmed / clarification requested | ≤ 5 business days |
| Fix for Critical | ≤ 7 calendar days |
| Fix for High | ≤ 14 calendar days |
| Fix for Medium | ≤ 30 calendar days |
| Fix for Low | ≤ 90 calendar days |
| Public disclosure (coordinated) | After fix ships + 7-day grace |

We target 90-day coordinated disclosure for all severities. If a fix will exceed these SLAs we communicate proactively.

### Safe Harbor

Bernstein follows responsible disclosure best practices. Researchers who:

- Report in good faith through the above channels
- Do not access, modify, or exfiltrate user data beyond the minimum needed to demonstrate impact
- Do not perform denial-of-service attacks against shared infrastructure
- Use the provided researcher sandbox (see below) rather than targeting production

will be treated as authorized testers. We will not pursue legal action for good-faith research that complies with these guidelines.

---

## Researcher Sandbox

A pre-configured, network-isolated Bernstein instance is available for security research.

### Quick start

```bash
git clone https://github.com/sipyourdrink-ltd/bernstein
cd bernstein
./scripts/researcher_sandbox.sh start
```

The script spins up a Docker Compose stack with:

- Task server on `http://localhost:18052` (separate port to avoid collisions)
- No outbound network access (firewall rules block egress)
- Ephemeral filesystem - nothing persists after `./scripts/researcher_sandbox.sh stop`
- Pre-loaded demo tasks and synthetic agent tokens for testing

See [`docs/security/bug-bounty.md`](docs/security/bug-bounty.md) for the full sandbox guide.

---

## Supported Versions

| Version | Supported |
|---------|-----------|
| 2.16.x  | Yes       |
| 2.15.x  | Critical patches only |
| < 2.15  | No        |

Security patches are backported to the current minor version only. Always run the latest release.

---

## Hall of Fame

Acknowledged researchers are listed in [`docs/security/security-acknowledgments.md`](docs/security/security-acknowledgments.md).

Thank you to everyone who has responsibly disclosed vulnerabilities.

---

## Engineering controls

Tracked here so the OSSF Scorecard can find them in one place. Workflow files
live under `.github/workflows/`.

### Static analysis (SAST)

| Tool       | Where                                                | Notes |
|------------|------------------------------------------------------|-------|
| CodeQL     | `.github/workflows/codeql.yml`                       | Python; runs on every PR + push to `main` and weekly cron. Config: `.github/codeql/codeql-config.yml`. |
| Bandit     | Pinned in `pyproject.toml` dev extras                | Runs in CI lint stage. |
| Semgrep    | Installed via `uv tool install semgrep` in CI        | Pattern packs run in the CI hardening stage. |
| SonarCloud | Project `sipyourdrink-ltd_bernstein` on SonarCloud   | Quality gate posts back to PRs via the `SONAR_TOKEN` secret. |

### Fuzzing and property tests

Property-based fuzzing lives in [`tests/property/`](tests/property/) and uses
[Hypothesis](https://hypothesis.readthedocs.io/). 48 modules cover the audit
chain, agent-card signing, atomic write paths, adapter spawn surface, and the
A2A protocol envelope. The suite runs as part of `scripts/run_tests.py` in CI.

Targeted regex-fuzz tests for the config parser live in
[`tests/unit/test_config_fuzz.py`](tests/unit/test_config_fuzz.py).

### Dependency hygiene

| Mechanism            | Where                                     | Cadence |
|----------------------|-------------------------------------------|---------|
| Dependabot (pip)     | `.github/dependabot.yml`                  | Weekly, 7-day cooldown, security-only group routed separately. |
| Dependabot (actions) | `.github/dependabot.yml`                  | Weekly Tuesday, 7-day cooldown. |
| `pip-audit`          | CI lint stage + ad-hoc maintainer sweeps  | OSV vulnerability service. |

Security-relevant deps (`cryptography`, `signxml`, `defusedxml`, `fastapi`,
`starlette`, `uvicorn`, `httpx`, `pyjwt`, `pyyaml`, `lxml`, `requests`,
`urllib3`, `bandit`, `pip-audit`) are pinned in the Dependabot `security` group
so CVE patches surface as standalone PRs rather than hiding inside feature
bumps.

### Code review

All non-trivial changes land via pull request with at least one approving
review. Security-touching changes (`SECURITY.md`, `.github/workflows/**`,
`src/bernstein/core/security/**`, signing keys, auth flows) need either two
reviewer approvals or operator-only direct push from a signed commit. Full
process: [`docs/CODE_REVIEW.md`](docs/CODE_REVIEW.md).

Branch protection on `main` enforces required status checks (CI, CodeQL,
SonarCloud) and at least one approving review before merge. Configuration is
maintained by repo admins via GitHub repo settings.

### OpenSSF Best Practices Badge

The CII / OpenSSF Best Practices badge (https://www.bestpractices.dev/) is a
self-attested questionnaire. Status: application in progress (maintainer
follow-up). Once the project is registered the badge will be added to the
README and linked here.

### Scorecard signals

The repo is scanned by the OpenSSF Scorecard via
`.github/workflows/scorecard.yml`. Results are uploaded to the GitHub
code-scanning dashboard. Current known gaps:

- **CIIBestPractices**: pending self-attestation (see above).
- **SignedReleases**: release tags are not yet cosign-signed; tracked in the
  evolution backlog.

Other Scorecard categories (CodeReview, Maintained, Vulnerabilities, Fuzzing,
SAST, CITests) are covered by the controls documented above.
