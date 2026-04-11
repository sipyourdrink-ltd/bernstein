# Bug Bounty Program

Full details of the Bernstein vulnerability disclosure and bug bounty program.

## Program overview

Bernstein orchestrates AI coding agents that run directly on a user's machine. The attack surface is meaningful: agents read/write files, execute CLI commands, and communicate via a local HTTP task server. We treat security seriously and compensate researchers who find real issues.

Reports go through **HackerOne**: https://hackerone.com/bernstein

Email fallback: security@bernstein.dev (see `SECURITY.md` for PGP key).

---

## Researcher sandbox

We provide a purpose-built Docker environment so researchers can explore the full attack surface without touching anyone else's infrastructure.

### Requirements

- Docker 24+ and Docker Compose v2
- 4 GB RAM available
- Ports `18052`, `18080` free on localhost

### Start the sandbox

```bash
git clone https://github.com/chernistry/bernstein
cd bernstein
./scripts/researcher_sandbox.sh start
```

This launches:

| Service | URL | Purpose |
|---------|-----|---------|
| Task server | http://localhost:18052 | Full Bernstein API |
| Sandbox dashboard | http://localhost:18080 | Web UI for task management |

### Pre-loaded test data

The sandbox starts with:

- 5 synthetic tasks in `open` state
- 3 demo agent tokens (`research-token-{1,2,3}`) with different privilege levels
- A demo project at `/sandbox/workspace/demo-project`

Use these tokens in your requests:

```bash
# List tasks
curl http://localhost:18052/tasks \
  -H "Authorization: Bearer research-token-1"

# Create a task
curl -X POST http://localhost:18052/tasks \
  -H "Authorization: Bearer research-token-1" \
  -H "Content-Type: application/json" \
  -d '{"title": "test task", "role": "backend"}'
```

### Network isolation

The sandbox container has **no outbound internet access**. The iptables rules applied by `researcher_sandbox.sh` block all egress except:

- DNS resolution (53/udp to the Docker DNS resolver)
- Inter-container traffic on the `research-net` bridge

This prevents accidental exfiltration and keeps the sandbox self-contained.

### Reset and cleanup

```bash
# Reset to clean state (wipes all tasks and worktrees)
./scripts/researcher_sandbox.sh reset

# Stop and remove all containers and volumes
./scripts/researcher_sandbox.sh stop
```

---

## What to look for

High-value targets in approximate priority order:

### 1. Task server authentication (Critical / High)

- `POST /tasks` — can an unauthenticated caller inject tasks?
- Token replay / forgery — are tokens validated correctly?
- Privilege escalation — can a `research-token-2` caller access admin endpoints?

Relevant code: `src/bernstein/core/routes/`

### 2. Agent spawner (Critical / High)

- Can a crafted task payload cause the spawner to execute arbitrary commands outside the workspace?
- Path traversal: does `scope` or `goal` field sanitize `../` sequences?
- Shell injection in `spawn_prompt` template expansion

Relevant code: `src/bernstein/core/spawner.py`, `src/bernstein/core/spawn_prompt.py`

### 3. Worktree isolation (High / Medium)

- Does the `EnterWorktree` / `ExitWorktree` flow prevent escaping the assigned worktree?
- Can one agent read another agent's worktree?

Relevant code: `src/bernstein/core/orchestrator.py`

### 4. Bulletin board (Medium)

- Can a bulletin post cause XSS in the web dashboard?
- Can a malicious agent post bulletins that influence other agents' behavior (prompt injection via bulletin)?

Relevant code: `src/bernstein/core/bulletin.py`

### 5. Docker sandbox itself (Medium / Low)

- Is the researcher sandbox actually isolated? Can a container process escape to the host?
- Does the resource cap (`--memory=2g --cpus=2`) prevent DoS?

---

## Submission guidelines

1. Use the HackerOne form at https://hackerone.com/bernstein
2. Include:
   - Description of the vulnerability
   - Steps to reproduce (curl commands, scripts, or a PoC)
   - Impact assessment (what an attacker could achieve)
   - Affected file(s) and line numbers if known
3. Attach screenshots or screen recordings for complex PoCs
4. Do not include production credentials — use the sandbox tokens

---

## Disclosure policy

We target coordinated disclosure: fix ships first, then you may publish. Timeline:

| Severity | Fix deadline | Disclosure after fix |
|----------|-------------|----------------------|
| Critical | 7 days | 7 days |
| High | 14 days | 7 days |
| Medium | 30 days | 14 days |
| Low | 90 days | 30 days |

If we miss a deadline we communicate proactively and you retain the right to disclose with 7 days' notice.

---

## Acknowledgments

Researchers who discover and responsibly disclose valid vulnerabilities are listed in
[`docs/security-acknowledgments.md`](security-acknowledgments.md).

We also issue CVEs for confirmed vulnerabilities of Medium severity and above.
