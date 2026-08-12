# Coordinated Disclosure

Full details of the Bernstein vulnerability disclosure process and researcher sandbox. For the policy summary, severity classification, and recognition, see [`SECURITY.md`](https://github.com/sipyourdrink-ltd/bernstein/blob/main/SECURITY.md).

## Program overview

Bernstein orchestrates AI coding agents that run directly on a user's machine. The attack surface is meaningful: agents read/write files, execute CLI commands, and communicate via a local HTTP task server. We treat security seriously and recognize researchers who find real issues.

Report a vulnerability by email to **forte@bernstein.run**, or open a private
security advisory on GitHub:
https://github.com/sipyourdrink-ltd/bernstein/security/advisories/new

The project publishes no PGP key, so email to that address is not
end-to-end encrypted. Use the GitHub private advisory for anything whose
contents matter before a fix ships - a working exploit, a credential, a
customer-identifying detail. Email is fine for everything else.

---

## Researcher sandbox

We provide a purpose-built Docker environment so researchers can explore the full attack surface without touching anyone else's infrastructure.

### Requirements

- Docker 24+ and Docker Compose v2
- 4 GB RAM available
- Ports `18052`, `18080` free on localhost

### Start the sandbox

```bash
git clone https://github.com/sipyourdrink-ltd/bernstein
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

- `POST /tasks` - can an unauthenticated caller inject tasks?
- Token replay / forgery - are tokens validated correctly?
- Privilege escalation - can a `research-token-2` caller access admin endpoints?

Relevant code: `src/bernstein/core/routes/`

### 2. Agent spawner (Critical / High)

- Can a crafted task payload cause the spawner to execute arbitrary commands outside the workspace?
- Path traversal: does `scope` or `goal` field sanitize `../` sequences?
- Shell injection in `spawn_prompt` template expansion

Relevant code: `src/bernstein/core/agents/spawner.py`, `src/bernstein/core/agents/spawn_prompt.py`

### 3. Worktree isolation (High / Medium)

- Does the `EnterWorktree` / `ExitWorktree` flow prevent escaping the assigned worktree?
- Can one agent read another agent's worktree?

Relevant code: `src/bernstein/core/orchestration/orchestrator.py`

### 4. Bulletin board (Medium)

- Can a bulletin post cause XSS in the web dashboard?
- Can a malicious agent post bulletins that influence other agents' behavior (prompt injection via bulletin)?

Relevant code: `src/bernstein/core/communication/bulletin.py`

### 5. Docker sandbox itself (Medium / Low)

- Is the researcher sandbox actually isolated? Can a container process escape to the host?
- Does the resource cap (`--memory=2g --cpus=2`) prevent DoS?

---

## Submission guidelines

1. Email **forte@bernstein.run** or open a private GitHub security advisory
2. Include:
   - Description of the vulnerability
   - Steps to reproduce (curl commands, scripts, or a PoC)
   - Impact assessment (what an attacker could achieve)
   - Affected file(s) and line numbers if known
3. Attach screenshots or screen recordings for complex PoCs
4. Do not include production credentials - use the sandbox tokens

---

## Disclosure policy

A first substantive response can take up to 90 days, and no fix date is
promised - severity sets the order work happens in, not a deadline. Bernstein
has one unpaid maintainer, and that is the whole reason.

You may disclose 90 days after your report, whether or not a fix has shipped,
without asking. Earlier is fine if we agree on it.

The terms that govern are in [`SECURITY.md`](../../SECURITY.md); this page is
the sandbox guide.

---

## Credit

An advisory is published when a fix ships, crediting the reporter by the name,
handle, or link they choose, and a CVE can be requested on it. The reporter is
also named in the release notes of the release carrying the fix.

Credit lives on those two artefacts rather than in a separate roll of honour, so
that it is produced by shipping the fix rather than by remembering to update a
page.
