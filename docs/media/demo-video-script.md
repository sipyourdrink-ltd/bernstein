# Demo Video Script: Bernstein Orchestrating 5 Agents

**Format**: Screen recording with narration  
**Duration**: ~3 minutes (180 seconds)  
**Target audience**: Developers who have heard of AI coding agents but haven't used an orchestrator  
**Goal**: Show the problem (one agent is slow, serial) → show the solution (5 parallel agents, done in seconds) → leave the viewer wanting to try it

---

## Pre-recording Checklist

- [ ] Terminal: font size 18+, dark theme, 120×40 columns
- [ ] Browser: SonarQube / task dashboard tab open but minimized
- [ ] Project: `bernstein demo` pre-run once so model weights are cached
- [ ] Silence notifications (macOS: Focus mode on)
- [ ] Microphone check: record 10 seconds, verify no background hum
- [ ] Screen resolution: 1920×1080 or 2560×1440 (do NOT use a scaled display — text blurs)
- [ ] Close all windows except the terminal (declutter)

---

## Scene 1 — The Problem (0:00–0:25)

**Screen**: Empty terminal. Type slowly.

```bash
# The old way: one agent, one task at a time
claude "Add JWT auth, tests, docs, security review, and a CI fix"
```

**Narration** (speak while the cursor blinks):

> "This is how most teams use AI coding agents today — one task, one agent, waiting. It works. But it's slow, it's serial, and you're paying for a Ferrari to idle in traffic."

**Action**: Hit `Ctrl+C` after 3 seconds. Don't wait for output.

**Narration**:

> "Bernstein changes the model. Instead of one agent doing everything, you get a conductor."

---

## Scene 2 — The Setup (0:25–0:45)

**Screen**: Show the project directory in tree form.

```bash
ls -1
```

Output visible: `bernstein.yaml`, `src/`, `tests/`, `docs/`

```bash
cat bernstein.yaml
```

**Narration** (while the file contents scroll):

> "Here's a real project. `bernstein.yaml` — a plain YAML file that describes goals as tasks. Each task has a role: backend, QA, docs, security, devops. Bernstein figures out which agents can work in parallel and which need to wait."

Show only the first 15 lines of the file — enough to see the structure, not overwhelming.

---

## Scene 3 — Launch (0:45–1:10)

**Screen**: Full-screen terminal.

```bash
bernstein run
```

**Narration** (as the task plan appears):

> "One command. Bernstein parses the plan, decomposes it into five tasks, and starts looking for available agents."

**What the viewer sees** (animated task plan in terminal):

```
Tasks created:
  [T-001] Implement JWT authentication middleware    role=backend    effort=medium
  [T-002] Write unit + integration tests for auth   role=qa         effort=low
  [T-003] Generate API docs with usage examples     role=docs       effort=low
  [T-004] Security review of auth implementation    role=security   effort=low
  [T-005] Fix CI pipeline — auth env vars missing   role=ci-fixer   effort=low
```

**Narration** (as agents spawn):

> "Five tasks. Five agents — picked from whatever CLI agents you have installed. In this case, three Claude Code instances and two Gemini CLI instances. Mixed models, mixed providers."

**What the viewer sees** (agent spawn lines):

```
Spawning agents...
  ▶ claude-backend   [claude-sonnet-4-6]  claimed T-001
  ▶ claude-qa        [claude-haiku-4-5]   claimed T-002
  ▶ gemini-docs      [gemini-3-flash]     claimed T-003
  ▶ gemini-security  [gemini-3-flash]     claimed T-004
  ▶ claude-cifixer   [claude-haiku-4-5]   claimed T-005
```

---

## Scene 4 — Live Activity Feed (1:10–2:00)

**Screen**: Live streaming log lines from all five agents simultaneously. Use `bernstein status --follow` or show the terminal output from `bernstein run`.

**Narration** (calm, let the activity speak):

> "All five agents working at once. This is real — no simulation. You're watching five separate terminal sessions running in parallel."

**Key log lines to highlight** (producer cuts or highlight with color during edit):

```
[00:04] backend    Creating src/auth/jwt.py...
[00:06] docs       Scanning existing routes...
[00:08] security   Reading src/auth/* for review...
[00:09] qa         Writing test_auth.py (12 test cases)...
[00:12] ci-fixer   Reading .github/workflows/ci.yml...
[00:14] backend    Adding middleware to FastAPI app...
[00:17] qa         Running pytest... 12 passed
[00:19] security   No hardcoded secrets found. TLS check passed.
[00:22] docs       Writing docs/api/auth.md...
[00:25] ci-fixer   Committing: fix(ci): add AUTH_SECRET_KEY to env
[00:27] backend    Committing: feat(auth): add JWT middleware
[00:29] docs       Committing: docs(api): auth endpoint reference
```

**Narration** (as logs scroll):

> "The QA agent starts writing tests before the backend agent is done — because it can read the interface specification from the task description. The security agent reviews the code as it's written, not after. The CI fixer found a missing environment variable on its own."

---

## Scene 5 — Verification (2:00–2:25)

**Screen**: The janitor summary appears.

```
Verifying results...
  ✓ Tests pass         (12/12)
  ✓ No regressions     (124 existing tests still pass)
  ✓ Security review    (no issues found)
  ✓ CI pipeline        (fixed)
  ✓ Docs committed     (docs/api/auth.md)
```

**Narration**:

> "Bernstein's janitor verifies the output. Tests pass. No regressions. Security review is clean. CI is fixed. The docs are committed."

**Screen**: Show the summary box.

```
╔═══════════════════════════════════════════╗
║  ✓ 5 tasks done   $0.61 spent   43s       ║
╚═══════════════════════════════════════════╝
```

**Narration** (emphasis on the numbers):

> "Five tasks. Forty-three seconds. Sixty-one cents."

Pause. Let that land.

---

## Scene 6 — Git History (2:25–2:45)

**Screen**: Show the git log.

```bash
git log --oneline -5
```

**Output**:

```
a3f9c1b feat(auth): add JWT middleware
b8e2d44 test(auth): 12 unit + integration tests
c1a5e7f docs(api): auth endpoint reference
d2b0e88 fix(security): reviewed auth for vulnerabilities
e5c3f29 fix(ci): add AUTH_SECRET_KEY to workflow env
```

**Narration**:

> "Clean git history. Each agent made its own commit with a descriptive message. Code review ready. No merge conflicts — Bernstein's merge queue handled sequencing."

---

## Scene 7 — The Call to Action (2:45–3:00)

**Screen**: Switch to a browser showing the Bernstein GitHub page or the quick-start command in the terminal.

```bash
pip install bernstein
bernstein -g "Add JWT auth with refresh tokens, tests, and API docs"
```

**Narration**:

> "Bernstein is open source. It works with Claude Code, Codex, Gemini CLI, Aider, Cursor, and a dozen other agents — mix and match. Install it now and run your first orchestrated job in under five minutes."

**On-screen text** (fade in at the end):

```
github.com/chernistry/bernstein
pip install bernstein
```

---

## Production Notes

### Recording the terminal

Use [VHS](https://github.com/charmbracelet/vhs) (`.tape` file) for reproducible terminal recordings, or OBS with a terminal theme. The `docs/assets/demo-runner.sh` script in this repo simulates the orchestration output and can be used to pre-record the activity feed section without needing live API keys.

```bash
# Preview the demo output locally (no API keys needed)
bash docs/assets/demo-runner.sh
```

### Editing

- Keep cuts tight — viewers drop off after 30 seconds of no new information
- Highlight agent log lines with colored overlays during Scene 4 (each role gets a color)
- Add captions — many developers watch on mute in Slack
- Do not add background music — it sounds unprofessional in technical content

### Voiceover vs. live narration

Record the narration separately and sync in post. This makes re-takes cheap and allows the terminal to run at real speed while narration timing is adjusted in edit.

### Uploading

- YouTube: title "Bernstein — 5 AI agents in 43 seconds" | description links to GitHub and docs
- Twitter/X: 60-second cut of Scenes 3–5 only (the action, no setup)
- Hacker News launch: embed the 60-second cut directly in the Show HN post

### What to avoid

- Don't show error recovery or retries unless you're making a specific point — it adds time and breaks flow
- Don't show the `bernstein.yaml` contents for more than 5 seconds — viewers can't read fast enough and will tune out
- Don't narrate what's already visible on screen — let log lines speak for themselves during Scene 4
