# Multi-Agent Swarm Architecture — Design Heritage

## Origin

Bernstein's architecture was battle-tested during a 48-hour sprint: 12 AI agents on a single laptop, 737 tickets closed (15.7/hour), 826 commits. Every design decision in Bernstein is a direct response to what worked and what broke during that sprint. This document captures the patterns.

---

## 1. Three-Tier Hierarchy

```
Human (CEO)
  └── VP agent (Opus — strategy, architecture, hard calls)
       └── Manager agent (Sonnet — operations, queue management, dispatch)
            ├── Specialist A (role: retrieval)
            ├── Specialist B (role: prompt engineering)
            ├── Specialist C (role: QA)
            └── ... (6-8 specialists total)
```

**Why 3 tiers**: Two-tier (human + agents) doesn't scale past 4 agents — the human becomes the bottleneck. Three tiers let the VP make strategic decisions (what to work on, when to kill a direction) while the manager handles operational dispatch (who gets which task). The human only intervenes for high-stakes decisions.

**Sweet spot**: 6-8 specialist agents on a single repo. Beyond 10, commit churn and merge conflicts outweigh the parallelism benefit.

---

## 2. File-Based Coordination

All coordination happens through files in `.sdd/`. No databases, no message queues, no APIs for state. This was the single most important architectural choice.

**Why files**: Every agent gets a fresh context window. The only reliable way to persist knowledge across agent lifetimes is to write it to disk. Files are:
- Git-native (merge conflicts = ownership violations caught automatically)
- Human-inspectable (you can `cat` any file to see system state)
- Crash-recoverable (process dies, files survive)
- Tool-agnostic (works with any CLI agent that can read/write files)

**Key files**:
- **Bulletin board** (append-only JSONL) — broadcast channel. Agents post findings, all agents read.
- **Directive** (single markdown) — current priorities. One file update reorients all agents instantly.
- **Task queue** (JSONL per agent) — pending/active/done tasks with priority and status.
- **Heartbeat** (JSON per agent) — "I'm alive, working on X." If stale for >10 min, agent is dead.

---

## 3. Agent Loop Protocol

Every agent runs the same loop:

```
LOOP:
  1. Read directive (priorities may have changed)
  2. Read own task queue — pick highest priority pending task
  3. If no pending tasks → request work from manager
  4. If manager unresponsive → escalate to VP
  5. Execute task
  6. Update task queue with result
  7. Update heartbeat
  8. Post to bulletin if finding is significant
  9. Commit work to git
  10. Exit (Bernstein spawns a fresh agent for the next task)
```

**Critical difference from long-running agents**: Steps 1-9 happen in a single agent session. The agent exits at step 10. Bernstein spawns a fresh agent for the next iteration. This prevents context rot, eliminates the "sleeping agent" resource waste, and makes crash recovery trivial — if an agent dies, its task is simply re-queued.

---

## 4. File Ownership

Each agent owns specific files. They may READ anything but WRITE only to their owned files. Shared files (bulletin) are append-only.

```
backend agent  → src/server.py, src/models.py, src/routes/
qa agent       → tests/, src/validators/
security agent → src/auth/, src/middleware/
docs agent     → docs/, README.md
```

**Why this matters**: Without ownership rules, 8 agents editing the same file creates merge hell. File ownership is Bernstein's primary conflict prevention mechanism. The orchestrator checks ownership before spawning — if two agents need the same file, their tasks are serialized.

---

## 5. What Worked

1. **Bulletin as broadcast**: All agents could see each other's findings. Cross-pollination was high. QA caught regressions that the original author missed.

2. **Directive as single truth**: When priorities shifted, one file update reoriented all agents instantly. No stale context, no out-of-date plans.

3. **Heartbeat for dead agent detection**: If heartbeat is >10 min old, agent is dead. Simple, reliable, no distributed coordination needed.

4. **Model mixing**: Opus for strategic/hard reasoning (VP, security). Sonnet for operational/fast tasks (manager, backend). Free-tier for trivial fixes. This was 3x more cost-effective than all-Opus.

5. **Kill criteria**: 2h with no progress → reassign. Any metric regression → immediate rollback. Confidence < 0.3 → kill without remorse. These rules prevented wasted compute on dead-end directions.

---

## 6. What Failed

1. **Enabling new components without testing**: A new LLM component was enabled without a small-slice test first → catastrophic regression (873/900 failures). **Rule: Never enable new components without a bounded test first.** This became Bernstein's sandbox validation pattern.

2. **Destructive transformations without gates**: A "cleanup" optimization accidentally wiped critical data. **Rule: Gate all destructive changes behind test verification.** This became Bernstein's janitor completion signals.

3. **Not all agents add value**: Some agents produced noise, not signal. **Rule: Kill underperformers early.** This informed Bernstein's evolve accept/reject pipeline.

4. **Background polling creates zombies**: Agents using background polling created processes that outlived their parent. **Rule: No background polling. Agents do work and exit.** This is why Bernstein uses short-lived agents.

5. **Too many agents on one branch**: 10+ agents on the same git branch = commit churn. **Optimal: 6-8 agents.** Bernstein defaults to `max_agents=6`.

---

## 7. How This Became Bernstein

| Sprint Pattern | Bernstein Feature |
|---------------|-------------------|
| File-based coordination | `.sdd/` state directory |
| Bulletin board | `POST /bulletin` API + bulletin.py |
| Directive / single truth | Task server as central authority |
| Heartbeat + dead detection | Agent reaping in orchestrator |
| File ownership | `owned_files` field on tasks |
| Model mixing | Tier-aware router (routing.yaml) |
| Kill criteria | Circuit breaker + evolution gate |
| Small-slice testing | Sandbox validation in evolve pipeline |
| Janitor verification | Completion signals (test_passes, path_exists) |
| Short-lived agents | Spawn → work → exit (no idle loops) |

---

*Architecture proven during Agentic RAG Legal Challenge 2026. 12 agents, 48 hours, 737 tasks completed.*
