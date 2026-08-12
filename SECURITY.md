# Security Policy

> Last reviewed: 2026-08-12.

## Reporting a Vulnerability

**Do not open a public issue for security vulnerabilities.**

### Preferred channel - Email

**forte@bernstein.run**

### Alternative - GitHub private security advisory

Open a private advisory so triage and the fix stay in one place:

**https://github.com/sipyourdrink-ltd/bernstein/security/advisories/new**

---

## What to expect

Bernstein is maintained by one person, unpaid, alongside other work. That is the
single fact this whole section follows from, so it is stated first rather than
buried.

**A first substantive response can take up to 90 days.** Not an
acknowledgement-then-silence — a reply that says whether the report is
confirmed, what the severity looks like, and what happens next. Often it will be
much sooner. Ninety days is the outer bound the project can actually stand
behind, and a bound that is met is worth more than a shorter one that is not.

**No fix date is promised.** Severity sets the order things get worked on, not a
deadline. If a fix is going to take a long time, the reply will say so rather
than leave it open.

**You may disclose 90 days after your report**, whether or not a fix has
shipped, without asking. That is not a concession extracted from us; it is the
counterpart of a slow response window, and it belongs in writing. Earlier is
fine too if we agree on it. A report that stays private indefinitely because a
volunteer was busy serves nobody, least of all the people running the software.

This replaces an earlier tiered timetable (72-hour triage, 7-day critical fixes,
and so on) that this project did not meet. It was removed rather than restated
because a policy nobody can hold to is worse than no policy.

### No bug bounty

There is no paid bounty program, no discretionary payment, and none is planned.
The project has no funding and no revenue. Reports are still genuinely welcome
and are read carefully — but if payment is what makes the work worthwhile for
you, this is not a program that can offer it, and it is better to say that
plainly than to leave it ambiguous.

### Credit

For a valid, first-reported issue: your name, handle, or link of your choosing
in the release notes of the release that ships the fix, and reporter credit on
the published GitHub Security Advisory, which can carry a CVE. Duplicate reports
are credited to the first valid reporter; an independent rediscovery is noted as
such.

---

## Coordinated Disclosure

### Scope

#### In scope

| Target | Notes |
|--------|-------|
| `github.com/sipyourdrink-ltd/bernstein` - Python package | `src/bernstein/` |
| Task server API (`localhost:8052` when self-hosted) | All HTTP endpoints |
| Agent spawner / orchestrator | Privilege escalation, task injection |
| Docker images (`bernstein:latest`, `bernstein:sandbox`) | Container escapes |
| CLI entry points (`bernstein run`, `bernstein serve`, etc.) | Argument injection |
| Authentication tokens (agent tokens, `BERNSTEIN_AUTH_TOKEN`) | Token forgery, replay |

#### Out of scope

- Third-party CLI agents (Claude Code, Codex, Gemini CLI) - report to their vendors
- The researcher's own sandboxed instance if improperly configured
- Social engineering / phishing attacks
- Denial-of-service against the public demo (rate-limit the test, not the server)
- Vulnerabilities in dependencies where no Bernstein-specific exploit path exists
- Reports that require physical access to the machine

### Severity

Confirmed reports are classified by CVSS to set the order they get worked on:

| Severity | CVSS | Examples |
|----------|------|----------|
| Critical | 9.0-10.0 | RCE on task server, container escape, token forgery enabling full takeover |
| High | 7.0-8.9 | Privilege escalation, unauthenticated task injection, path traversal outside workspace |
| Medium | 4.0-6.9 | Auth bypass for low-privilege endpoints, info disclosure of agent tokens, SSRF |
| Low | 0.1-3.9 | Minor info disclosure, non-exploitable misconfigurations |

### Safe Harbor

Researchers who:

- Report in good faith through the above channels
- Do not access, modify, or exfiltrate user data beyond the minimum needed to demonstrate impact
- Do not perform denial-of-service attacks against shared infrastructure
- Use the provided researcher sandbox (see below) rather than targeting production

will be treated as authorized testers. We will not pursue legal action for
good-faith research that complies with these guidelines. This part carries no
resource constraint and is not qualified by anything above it.

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
| 3.14.x  | Yes       |
| < 3.14  | No        |

Fixes ship on the current release line. There is no backport branch.
