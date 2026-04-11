# Security Policy

## Reporting a Vulnerability

**Do not open a public issue for security vulnerabilities.**

### Preferred channel — Bug Bounty

Submit reports through our HackerOne program:

**https://hackerone.com/bernstein**

HackerOne handles triage, communication, and rewards. Reports are triaged within 72 hours.

### Alternative — Email

For issues outside the HackerOne scope or if you prefer direct contact:

**security@bernstein.dev** (PGP key: `/.well-known/security-pgp.asc`)

---

## Bug Bounty Program

### Scope

#### In scope

| Target | Notes |
|--------|-------|
| `github.com/chernistry/bernstein` — Python package | `src/bernstein/` |
| Task server API (`localhost:8052` when self-hosted) | All HTTP endpoints |
| Agent spawner / orchestrator | Privilege escalation, task injection |
| Docker images (`bernstein:latest`, `bernstein:sandbox`) | Container escapes |
| CLI entry points (`bernstein run`, `bernstein server`, etc.) | Argument injection |
| Authentication tokens (agent tokens, `BERNSTEIN_AUTH_TOKEN`) | Token forgery, replay |

#### Out of scope

- Third-party CLI agents (Claude Code, Codex, Gemini CLI) — report to their vendors
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

Rewards are paid in USD via HackerOne. Minimum payout threshold: $25.

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
git clone https://github.com/chernistry/bernstein
cd bernstein
./scripts/researcher_sandbox.sh start
```

The script spins up a Docker Compose stack with:

- Task server on `http://localhost:18052` (separate port to avoid collisions)
- No outbound network access (firewall rules block egress)
- Ephemeral filesystem — nothing persists after `./scripts/researcher_sandbox.sh stop`
- Pre-loaded demo tasks and synthetic agent tokens for testing

See [`docs/bug-bounty.md`](docs/bug-bounty.md) for the full sandbox guide.

---

## Supported Versions

| Version | Supported |
|---------|-----------|
| 1.4.x   | Yes       |
| 1.3.x   | Critical patches only |
| < 1.3   | No        |

Security patches are backported to the current minor version only. Always run the latest release.

---

## Hall of Fame

Acknowledged researchers are listed in [`docs/security-acknowledgments.md`](docs/security-acknowledgments.md).

Thank you to everyone who has responsibly disclosed vulnerabilities.
