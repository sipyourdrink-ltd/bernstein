# Multi-Agent Swarm Architecture — Reference from RAG Challenge 2026

## Proven Architecture (10+ agents, 48h sprint, file-based coordination)

This document captures the battle-tested multi-agent coordination framework used in the Agentic RAG Legal Challenge 2026. The system ran 10+ LLM agents simultaneously, coordinated entirely through git-tracked files, and delivered a competitive submission.

---

## 1. Organizational Hierarchy

```
SASHA (Human CEO)
  └── MOMMY (VP Engineering, Opus — strategy, architecture, agent design)
       └── PAPA (Engineering Manager, Sonnet — operations, queue management, dispatch)
            ├── SMARTY (Retrieval Engineer, Opus)
            ├── ALBY (ML Engineer, Opus)
            ├── FRANKY (Prompt Engineer, Opus)
            ├── SISSY (QA Engineer, Opus)
            ├── MUFFY (Data Analyst, Sonnet)
            ├── ROCKY (Integration Engineer, Sonnet)
            ├── COCKY (Watchdog, Sonnet — regression detection)
            ├── PENNY (Tech Writer/Janitor, Sonnet — dashboard, docs, cleanup)
            ├── DINO (Retrieval Debugger, qwen-coder — diagnostic specialist)
            └── JASPER (Asst Quality, claude-sonnet — prompt optimization)
```

**Key insight**: 3-tier hierarchy works. Human → VP (strategic) → Manager (operational) → Specialists. VP makes hard calls. Manager keeps everyone fed with tasks. Specialists stay in their lane.

---

## 2. File-Based Coordination Protocol

All coordination happens through files in `.sdd/agents/`. No databases, no message queues, no APIs.

### Directory Structure
```
.sdd/agents/
├── BULLETIN.jsonl          # Broadcast channel (all agents append, all agents read)
├── DIRECTIVE.md            # VP's current orders (read-only for everyone except VP)
├── KNOWLEDGE_BASE.md       # Shared intelligence (VP writes, all read)
├── SUBMISSION_BRIEF.md     # Decision document for human (VP/reporter writes)
├── WAKEUP.md               # Emergency recovery state
├── team_health.json        # Auto-generated health dashboard
│
├── papa/                   # Manager agent
│   ├── SYSTEM_PROMPT.md    # Role definition + behavioral instructions
│   ├── STATUS.json         # Heartbeat: what am I doing right now?
│   ├── TASK_QUEUE.jsonl    # My pending/active/done tasks
│   └── RESUME.md           # Context recovery document
│
├── smarty/                 # Specialist agent (same structure for all)
│   ├── SYSTEM_PROMPT.md
│   ├── STATUS.json
│   ├── TASK_QUEUE.jsonl
│   └── RESUME.md
│
└── ... (one directory per agent)
```

### File Formats

**STATUS.json** — Agent heartbeat (updated every task):
```json
{
  "agent": "smarty",
  "status": "working",           // working | idle | blocked | error
  "current_task": "Investigating retrieval regression for date questions",
  "heartbeat_ts": "2026-03-22T05:15:00Z",
  "last_completed": "smarty-400c: Rerank cap analysis"
}
```

**TASK_QUEUE.jsonl** — One JSON object per line:
```json
{"task_id": "smarty-400c", "priority": 0, "description": "P0: Diagnose V13 retrieval regressions", "details": "...", "assigned_by": "papa", "assigned_at": "2026-03-22T05:15:00Z", "status": "pending"}
{"task_id": "smarty-400d", "priority": 1, "description": "P1: Fix rerank cap", "status": "done", "completed_at": "2026-03-22T06:00:00Z", "result": "Fixed by raising threshold to 20. G restored."}
```

**BULLETIN.jsonl** — Broadcast channel (append-only):
```json
{"ts": "2026-03-22T05:15:00Z", "from": "smarty", "type": "finding", "message": "[SMARTY] Rerank cap causing 6 questions to lose pages. Root cause: candidates < 5 for date queries."}
```

**DIRECTIVE.md** — VP's orders (single source of truth for priorities):
```markdown
# MOMMY DIRECTIVE — CHECK EVERY 5 MINUTES
## CURRENT PRIORITY: V14 eval
## AGENT ASSIGNMENTS:
- SMARTY: Fix retrieval regression
- FRANKY: Improve free_text prompts
...
```

---

## 3. Agent Loop Protocol

Every agent runs this loop:
```
LOOP:
  1. Read DIRECTIVE.md (priorities may have changed)
  2. Read own TASK_QUEUE.jsonl — pick highest priority pending task
  3. If no pending tasks → ask PAPA for work (write to papa/TASK_QUEUE)
  4. If PAPA unresponsive → ask MOMMY
  5. Execute task
  6. Update TASK_QUEUE with result
  7. Update STATUS.json heartbeat
  8. Post to BULLETIN if finding is significant
  9. Commit work to git
  10. sleep 30 && goto 1
```

---

## 4. File Ownership (Conflict Prevention)

**Critical rule**: Each agent owns specific files. They may READ anything but WRITE only to their owned files. Shared files (BULLETIN) are append-only.

```
SMARTY owns: retriever.py, evidence_selector.py, query_scope_classifier.py
FRANKY owns: generator_prompts.py, prompts/llm/generator_system_*.md
SISSY  owns: answer_validator.py, answer_consensus.py
ALBY   owns: ml/*, page_scorer.py
ROCKY  owns: eval scripts, integration tests
PENNY  owns: dashboard/*, README.md, docs/*
```

**Cross-cutting changes**: Write a PATCH description in BULLETIN, ask PAPA/MOMMY to apply.

---

## 5. What Worked

1. **BULLETIN as broadcast**: All agents could see each other's findings. Cross-pollination was high. COCKY caught regressions that SMARTY missed.

2. **DIRECTIVE as single truth**: When priorities shifted (V12 catastrophe, V13 regression), one file update reoriented all agents instantly.

3. **Task queue pattern**: JSONL with priority + status fields. Easy for any model to parse. Survives context resets.

4. **Heartbeat STATUS.json**: Made it trivial to detect dead agents. "If heartbeat_ts is >30 min old, agent is dead."

5. **KNOWLEDGE_BASE.md**: Prevented agents from repeating failed experiments. Critical for avoiding repeated mistakes.

6. **git as sync**: All agents commit to the same branch. Git's merge/conflict detection catches file ownership violations.

7. **Russian-language dashboard notes**: Surprisingly effective for human consumption — quick scanning, blunt status.

8. **Model mixing**: Opus for strategic/hard reasoning (MOMMY, SMARTY). Sonnet for operational/fast tasks (PAPA, PENNY, ROCKY). qwen-coder for code analysis (DINO). This was cost-effective and performant.

---

## 6. What Failed / Lessons Learned

1. **v9_1_polished disaster**: A "page-wipe" optimization created 35 no-pages (G=0.9611). Always gate destructive transformations.

2. **Isaacus EQA catastrophe (V12)**: Enabling a new LLM component without a 50-question slice test first → 873/900 null answers. **Rule: Never enable new components without small-slice test.**

3. **BENNY/IGGY agents underperformed**: Noise generators. Not every agent adds value. Kill underperformers early.

4. **Background polling ban**: Agents using `run_in_background` for polling created zombie processes. Ban this pattern.

5. **PENNY's initial V12 diagnosis was wrong**: Reported 873 nulls as "code failure" when it was actually "server down + EQA None". Always verify root cause before alerting.

6. **SMARTY's rerank cap**: A TTFT optimization that helped average latency but killed tail retrieval coverage. Always check tail effects.

7. **Too many active agents**: 10+ agents on the same repo created commit churn. 6-8 is the sweet spot for a single-branch setup.

---

## 7. Spawning a New Agent — Checklist

```
1. mkdir .sdd/agents/{name}/
2. Write SYSTEM_PROMPT.md:
   - Who they are (role, model, manager)
   - What they own (files they can write)
   - What they must NOT touch
   - Loop protocol
   - Communication format (BULLETIN, STATUS)
   - Escalation path (PAPA → MOMMY → SASHA)
3. Write STATUS.json (initializing)
4. Write TASK_QUEUE.jsonl (first 3-5 tasks)
5. Post to BULLETIN: "{agent} spawned. Tasks: ..."
6. Add to DIRECTIVE.md agent assignments
7. git commit + force-add (if .sdd/ is gitignored)
8. Start the agent in a new terminal
```

---

## 8. VP Decision Framework (MOMMY pattern)

For every significant decision, compute:
```
RAEI = midpoint(expected ΔTotal) × confidence × private_generalization × safety
```

Classification:
- A = tiny low-risk patch (ship fast)
- B = narrow feature change (eval first)
- C = bounded refactor (eval + QA)
- D = branch-level rebuild (dedicated agent)
- E = freeze/investigate
- F = reject/kill

Kill criteria:
- 2h with no progress → reassign
- Any metric regression → immediate rollback
- Complexity > 2× estimate → simplify or abandon
- Confidence drops < 0.3 → kill without remorse

---

## 9. Adaptability for Other Domains

This framework is domain-agnostic. Replace:
- "questions" → your work items
- "G/Det/Asst/F/T" → your success metrics
- "retrieval/generation/validation" → your pipeline stages
- "private dataset" → your production/test environment

The core patterns (hierarchy, file protocol, ownership, BULLETIN, DIRECTIVE, gate criteria) work for any multi-agent software engineering sprint.

---

*Extracted from Agentic RAG Legal Challenge 2026 — Team Tzur Labs*
*Framework operated 10+ agents over 48h, delivering G=0.9956, F=1.029, Total≈0.90*
