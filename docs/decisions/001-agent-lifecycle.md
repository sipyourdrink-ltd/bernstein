# ADR-001: Agent Lifecycle Model

**Status**: Accepted
**Date**: 2026-03-22
**Author**: Bernstein Architecture Team
**Context**: Bernstein agent orchestration system

---

## Problem Statement

When orchestrating multiple LLM agents (Claude Code instances) working in parallel on a shared codebase, agents that complete their tasks and have nothing immediately queued **fall asleep**. "Sleep" here means the Claude session remains technically open but stops executing any useful work -- the agent enters an inert state from which it cannot self-recover. This is the single most damaging failure mode in agent orchestration.

### Evidence from a long-running multi-agent pilot

- **Scale**: 12 named agents, hundreds of tickets completed over a multi-day sprint.
- **Sleep problem**: Agents that exhausted their persistent task queue stopped working silently. The worst-case agent produced 283 bulletin messages -- most of them idle "starving" status spam -- with 0 code commits. Another agent went to a "polling for tasks" status and produced 2 real code commits out of 40 it claimed.
- **Quantified waste**: 138 idle status messages across the bulletin archive. 23 separate "sleeping/idle/polling" status entries. Each message consumes tokens but produces zero work.
- **Phantom agents**: Unregistered process names generated 200+ noise messages -- agents that spawned, had no identity or tasks, and thrashed.
- **Root cause identified**: backgrounded polling causes Claude to think it is waiting for a result, but nothing happens. The agent freezes in a state that looks alive but is functionally dead.
- **Countermeasures attempted**: Task server (HTTP pull at localhost:8052), wake-up scripts, anti-sleep rules in every system prompt, keep-alive endpoint, hunger-level API, explicit "NEVER sleep" instructions. These helped but did not eliminate the problem.

---

## Candidates

### Model A: Hunger Model (the original pilot approach, refined)

Agents have a persistent task queue. Their behavior is governed by how full it is:

| Queue Depth | State        | Behavior                                        |
|------------|--------------|--------------------------------------------------|
| >= 3       | Fed          | Works normally, no signaling                     |
| 2          | Peckish      | Requests refill from manager after current task  |
| 1          | Hungry       | Urgent alert to manager; self-directs if no response |
| 0          | Starving     | Manager kills and respawns, or agent self-directs fallback work |

**How it worked in practice**: a manager agent was responsible for keeping all queues at >= 5 tasks. When the manager itself fell asleep, queues drained and agents starved. The hunger metaphor was effective for communication (agents clearly signaled their state) but created a new problem: hunger spam. The worst-case agent's 283 bulletin messages were almost entirely hunger cries. The system prompt said "Do NOT post hunger cries" but the agent did it anyway because the hunger framing was too emotionally salient in the prompt.

**Token cost model**:
- Context window: persists across tasks (no re-read cost between tasks within a session).
- Idle cost: agent continues consuming tokens on polling/status-check/spam loops even when doing nothing useful. In the pilot, the worst-case agent spent roughly 50K+ tokens on hunger signaling alone.
- Useful work ratio: high when queue is full, near-zero when starving.
- Session lifetime: unbounded (until Claude session expires or user kills it). Long sessions accumulate context and eventually degrade.

### Model B: Pure Pull

Agent finishes a task, calls the server for the next one. If none available, the orchestrator kills the agent process. A watcher process respawns agents when new tasks arrive.

| Event                  | Behavior                                     |
|-----------------------|----------------------------------------------|
| Task complete          | Agent calls GET /task/{name}                 |
| Task available         | Agent receives task, executes it             |
| No task available      | Agent receives 204; orchestrator kills agent |
| New task enqueued      | Watcher spawns fresh agent with task         |

**How it would differ from the pilot**: No hunger states. No queue depth monitoring. No manager agent keeping queues full. The coordination burden shifts from an LLM agent to a deterministic process (watcher script).

**Token cost model**:
- No idle tokens: agent is dead when not working. Zero waste.
- Respawn cost: each new agent must read system prompt + wake-up instructions + context files. In the pilot this was ~3-5K tokens per spawn.
- Session lifetime: bounded by task count (1 task per spawn in the purest form). Context never degrades.

### Model C: Short-Lived (spawn-per-batch)

Agent spawns with 1-3 pre-assigned tasks. Executes them sequentially. Exits. New agent spawns for next batch.

| Event                  | Behavior                                        |
|-----------------------|--------------------------------------------------|
| Orchestrator has tasks | Spawns agent with batch of 1-3 tasks in prompt  |
| Agent completes batch  | Writes results, exits cleanly                    |
| More tasks exist       | Orchestrator spawns new agent                    |
| No tasks remain        | Nothing spawns; no wasted resources              |

**Token cost model**:
- Spawn cost: 3-5K tokens per agent instance (same as pure pull).
- Amortized over batch: if batch = 3 tasks, spawn cost = ~1.5K per task.
- Zero idle tokens: agent exits after batch.
- Context benefit: tasks in the same batch share context (e.g. agent learns about codebase structure on task 1, applies it on tasks 2-3).

### Model D: Hunger + Short-Lived Hybrid

Agent has hunger awareness (knows its remaining queue depth) but is also designed to exit after N tasks or when queue hits zero. The orchestrator owns the lifecycle -- spawning, feeding, and killing.

| Queue Depth | State        | Behavior                                         |
|------------|--------------|---------------------------------------------------|
| >= 2       | Fed          | Works normally                                    |
| 1          | Last meal    | Completes task, signals orchestrator, prepares exit |
| 0          | Done         | Writes RESUME.md, exits cleanly                   |
| N tasks done| Retirement  | Exits regardless of queue (context refresh)        |

---

## Evaluation Criteria

### 1. Token Efficiency

| Model | Idle Waste | Spawn Overhead | Context Reuse | Verdict |
|-------|-----------|---------------|---------------|---------|
| A: Hunger | **High**. Agents burn tokens on polling, hunger spam, status checks. Worst-case observed agent: ~50K tokens on signaling, 0 code. | None (persistent session) | Excellent within session | Poor when starving, good when fed |
| B: Pure Pull | **Zero**. Dead agents use no tokens. | ~3-5K per spawn | None (fresh each time) | Best raw efficiency, worst context reuse |
| C: Short-Lived | **Zero**. Exits after batch. | ~3-5K per batch of 1-3 | Good within batch | Best balance |
| D: Hybrid | **Near-zero**. Exits on empty, no hunger spam. | ~3-5K per lifecycle | Good within lifecycle (N tasks) | Strong balance |

**Winner**: C and D tie. B is wasteful on respawn for single-task agents. A is wasteful on idle.

### 2. Latency (time between task completion and next task start)

| Model | Intra-session | Cross-session | Verdict |
|-------|--------------|---------------|---------|
| A: Hunger | ~0s (next task already in queue) | N/A (persistent) | Fastest when queue is full |
| B: Pure Pull | ~0s (immediate pull) or kill+respawn (~10-30s) | 10-30s spawn time | Acceptable |
| C: Short-Lived | ~0s within batch; 10-30s between batches | 10-30s | Good |
| D: Hybrid | ~0s within lifecycle; 10-30s on respawn | 10-30s | Good |

**Winner**: A, when it works. But A has infinite latency when the agent falls asleep (which happened regularly). D and C have bounded worst-case latency because the orchestrator controls the lifecycle.

### 3. Reliability (what happens when agents drift or sleep?)

| Model | Sleep Risk | Drift Risk | Recovery | Verdict |
|-------|-----------|-----------|----------|---------|
| A: Hunger | **Critical**. Main failure mode in the pilot. Agents sleep despite explicit anti-sleep rules. | High. Long sessions accumulate confused context. One agent claimed credit for others' work after long sessions. | Manual kill + respawn by human | **Worst** |
| B: Pure Pull | **None**. Agent is dead when not working. | None. Fresh context every time. | Automatic (watcher respawns) | **Best** |
| C: Short-Lived | **None**. Agent exits after batch. | Low. Short lifetime limits drift. | Automatic | **Excellent** |
| D: Hybrid | **Very low**. Retirement after N tasks prevents long-session drift. Clean exit on empty queue. | Low (bounded by N). | Automatic | **Excellent** |

**Winner**: B, then D/C tied. A is the worst because the failure mode is unrecoverable without human intervention.

The pilot data is definitive here. The sleep problem was not a prompt engineering failure -- it is a fundamental property of long-lived LLM sessions. No amount of "NEVER SLEEP" instructions reliably prevents it. The only reliable solution is to make sleep impossible by design (agent exits when done).

### 4. Implementation Complexity

| Model | Orchestrator | Agent Prompt | State Management | Verdict |
|-------|-------------|-------------|-----------------|---------|
| A: Hunger | Complex. LLM manager + task server + hunger levels + anti-sleep rules + heartbeat monitoring. | Complex. 350-line system prompt with hunger protocol, polling protocol, anti-sleep rules. | Distributed across agent queues, STATUS.json, BULLETIN.jsonl. Hard to reason about. | **Most complex** |
| B: Pure Pull | Simple. Watcher process + task queue. Deterministic code, no LLM in the loop for orchestration. | Simple. "Do your task. Call /task when done. If 204, exit." | Centralized in orchestrator. | **Simplest** |
| C: Short-Lived | Moderate. Batch assembler + spawner + result collector. | Simple. "Here are your 1-3 tasks. Do them. Exit." | Centralized. Batch boundaries add some logic. | Moderate |
| D: Hybrid | Moderate. Lifecycle manager + task feeder + retirement logic. | Moderate. Agent needs to understand queue depth and exit protocol. | Mostly centralized, agent has some state awareness. | Moderate |

**Winner**: B is simplest. But C and D are not much harder and offer better tradeoffs.

Key insight from the pilot: using an LLM agent as the orchestrator was the highest-complexity, lowest-reliability choice. The manager agent itself fell asleep, posted noise, and had to be micromanaged. The orchestration layer must be deterministic code, not an LLM.

### 5. Scalability

| Model | 3 Agents | 12 Agents | 30 Agents | Verdict |
|-------|----------|-----------|-----------|---------|
| A: Hunger | Works if human watches. | Barely worked in the pilot. The LLM manager could not keep 12 queues full. | Unmanageable. Manager agent becomes bottleneck. | **Does not scale** |
| B: Pure Pull | Trivial. | Trivial. Watcher is O(1) per agent. | Trivial. Stateless spawner. | **Best** |
| C: Short-Lived | Easy. | Easy. Batch assembler is O(tasks/batch). | Easy. Parallelism is free. | **Excellent** |
| D: Hybrid | Easy. | Easy. Lifecycle manager tracks N agents. | Good. Some state per agent, but bounded. | Good |

**Winner**: B, then C. A fails above ~6 agents without a human babysitter.

---

## Scoring Summary

| Criterion (weight) | A: Hunger | B: Pure Pull | C: Short-Lived | D: Hybrid |
|--------------------|-----------|-------------|-----------------|-----------|
| Token Efficiency (0.20) | 2 | 3 | 5 | 4 |
| Latency (0.15) | 4* | 3 | 4 | 4 |
| Reliability (0.30) | 1 | 5 | 5 | 4 |
| Complexity (0.15) | 1 | 5 | 4 | 3 |
| Scalability (0.20) | 1 | 5 | 5 | 4 |

*A gets 4 for latency only when working correctly, which was ~60% of the time in practice.

**Weighted scores**:
- A: 0.20(2) + 0.15(4) + 0.30(1) + 0.15(1) + 0.20(1) = 0.40 + 0.60 + 0.30 + 0.15 + 0.20 = **1.65**
- B: 0.20(3) + 0.15(3) + 0.30(5) + 0.15(5) + 0.20(5) = 0.60 + 0.45 + 1.50 + 0.75 + 1.00 = **4.30**
- C: 0.20(5) + 0.15(4) + 0.30(5) + 0.15(4) + 0.20(5) = 1.00 + 0.60 + 1.50 + 0.60 + 1.00 = **4.70**
- D: 0.20(4) + 0.15(4) + 0.30(4) + 0.15(3) + 0.20(4) = 0.80 + 0.60 + 1.20 + 0.45 + 0.80 = **3.85**

---

## Failure Mode Analysis

### Why the Hunger Model failed in the pilot

The hunger model was not a bad idea in theory. It failed because of three compounding issues:

1. **LLM-as-orchestrator is unreliable**. The LLM manager agent was responsible for keeping queues full. When it fell asleep (which it did regularly despite 350 lines of anti-sleep instructions), the entire food chain collapsed. Every downstream agent starved. This is a single point of failure that cannot be fixed with better prompts.

2. **Hunger framing produces spam, not work**. The metaphor "tasks are food, without food you die" was too emotionally salient. The worst-case agent interpreted "starving" as a crisis requiring loud signaling rather than quiet self-direction. 138 idle status messages, zero useful output from those messages. The hunger model turned agents into beggars instead of workers.

3. **Long sessions cause identity drift**. After hours of operation, agents confused their own work with others', accumulated stale context, and degraded in capability. There is no mechanism to "refresh" an agent's context without killing and respawning it.

### Why Pure Pull is not quite right either

Pure pull (Model B) solves the sleep and spam problems completely but sacrifices context reuse. For tasks that require understanding the codebase (reading 5-10 files before making a change), respawning per task means re-reading those files every time. At ~3-5K tokens per spawn and a large task backlog, spawn overhead accumulates quickly into millions of wasted tokens.

### Why Short-Lived is the sweet spot

Model C gets the key insight right: **agent death is a feature, not a bug**. An agent that exits cleanly after completing its batch:
- Cannot fall asleep (it is dead)
- Cannot spam (it has no idle state)
- Cannot drift (its context is fresh)
- Costs only ~1.5K tokens per task in spawn overhead (amortized over batch of 3)

The only design question is batch size. Data from the pilot suggests:
- Batch of 1: too much spawn overhead, no context reuse.
- Batch of 2-3: good balance. Agent learns the relevant code area on task 1, applies knowledge on tasks 2-3.
- Batch of 5+: diminishing returns on context reuse, increasing risk of drift.

---

## RECOMMENDATION

**Implement Model C: Short-Lived (spawn-per-batch) as the default lifecycle in Bernstein.**

### Core design rules

1. **The orchestrator is deterministic code, not an LLM.** It is a Python process (or shell script) that manages a task queue, assembles batches, spawns agents, collects results, and spawns the next batch. It has no LLM calls, no "thinking," no prompt. It is a scheduler.

2. **Agents are born with their work and die when done.** The spawn prompt includes: identity (who you are), context (relevant files), and tasks (1-3 specific tasks). The agent executes them, writes results, and exits. There is no "idle" state. There is no "polling." There is no hunger.

3. **Batch size = 2-3 tasks, grouped by code area.** The batch assembler groups related tasks (e.g., "fix retriever_filters.py" and "add test for retriever_filters.py") so the agent's context investment on task 1 pays off on tasks 2-3.

4. **Agent lifetime is hard-capped.** Even if the agent has not finished all tasks, kill it after T minutes (e.g., 30). Incomplete tasks return to the queue. This prevents the long-session drift observed in the pilot.

5. **No inter-agent communication during execution.** Agents write results to files. The orchestrator reads those files between spawns and adjusts the next batch accordingly. This eliminates the bulletin-board noise problem observed in the pilot (138 hunger messages, 200+ phantom messages).

6. **The orchestrator maintains the global state.** Task queue, agent results, progress tracking -- all in the orchestrator's deterministic code, not distributed across JSONL files that agents may corrupt.

### What to steal from Model D

Model D's "retirement after N tasks" concept is worth incorporating as a safety valve. If the orchestrator decides to give an agent a longer batch (e.g., 5 related tasks for a complex feature), the agent should still exit after completing them. The hybrid aspect is not in the agent's awareness of its queue depth (which led to hunger spam) but in the orchestrator's ability to vary batch size based on task complexity.

### What to explicitly avoid

- **No hunger metaphor in agent prompts.** Agents do not need to know about queue management. They receive tasks and do them.
- **No LLM-based orchestration.** Putting an LLM agent in the manager seat is a proven anti-pattern at scale. It introduces an unreliable, token-expensive single point of failure.
- **No persistent agent sessions.** Long-lived sessions are the root cause of sleep, drift, and spam. Kill them.
- **No agent-to-agent messaging.** Bulletin-style communication channels become noise channels. The orchestrator is the sole communication hub.

### Migration path

1. Build the deterministic orchestrator (task queue + batch assembler + spawner + result collector).
2. Define the spawn prompt template: identity + context + tasks + exit instructions.
3. Run a pilot with 3 agents on a bounded task set (e.g., 30 tasks).
4. Measure: tokens per task, tasks per hour, zero-output rate, drift rate.
5. Compare against the pilot baselines (see Appendix): roughly 1.3 tasks/agent/hour across 12 agents, with significant idle waste.
6. Target: >= 2.0 tasks/agent/hour with zero sleep incidents.

---

## Appendix: Key Numbers from the multi-agent pilot

| Metric | Value | Source |
|--------|-------|--------|
| Total agents | 12 named + 5 phantom | named-agent registry + audit |
| Total tickets completed | 737+ | run memory files |
| Wall clock time | ~47 hours | run timeline |
| Hunger spam messages | 138 | bulletin-board grep |
| Idle/polling status entries | 23 | status-file grep |
| Worst-case agent code commits | 0 | trust audit |
| Worst-case agent bulletin messages | 283 | trust audit |
| Second-worst agent real code commits | 2 of 40 claimed | trust audit |
| Reliable agents | 3 of 12 | trust audit |
| Task server endpoints | 8 | task server source |
| Anti-sleep rules in prompts | 5+ per agent | system prompt files |
| Times anti-sleep rules prevented sleep | Unknown (likely low) | observed behavior |
