# ADR-004: File-Based State via `.sdd/`

**Status**: Accepted  
**Date**: 2026-03-22  
**Context**: Bernstein multi-agent orchestration system

---

## Problem

A multi-agent orchestration system needs shared state: the task queue, agent
assignments, progress tracking, metrics, and results. How that state is stored
determines whether the system is inspectable, recoverable, and debuggable.

The options are roughly: in-memory (fast, lost on crash), a database (durable,
requires infrastructure), or files (durable, no infrastructure).

---

## Decision

All persistent Bernstein state lives in `.sdd/` — plain text files (YAML,
Markdown, JSONL, JSON) on the local filesystem. No embedded database. No hidden
in-memory state.

```
.sdd/
├── backlog/
│   ├── open/          ← YAML task files waiting to run
│   ├── claimed/       ← tasks currently assigned to an agent
│   ├── done/          ← completed tasks with result summaries
│   └── failed/        ← failed tasks with error details
├── runtime/           ← ephemeral (PIDs, logs, signals, heartbeats)
│   ├── tasks.jsonl    ← recovery checkpoint for the task server
│   ├── logs/          ← per-agent log files
│   └── signals/       ← WAKEUP / SHUTDOWN / HEARTBEAT signal files
└── metrics/
    ├── tasks.jsonl    ← per-task timing, cost, token usage
    └── agents.jsonl   ← per-agent session metrics
```

**`runtime/` is ephemeral**. It holds PIDs, logs, and in-flight signals. Do not
commit it. Delete it freely when stopping Bernstein.

**`backlog/` and `metrics/` are durable**. Back them up with your code. Commit
`backlog/` to track task history alongside the code that resolved it.

---

## Rejected alternatives

### Option A: SQLite embedded database

SQLite would give us atomic transactions, indexed queries, and a familiar query
interface. Several task management tools use this approach.

**Why rejected:**
- Not inspectable without tooling. `cat .sdd/backlog/open/my-task.yaml` is
  instant; `sqlite3 .sdd/state.db "SELECT * FROM tasks WHERE status='open'"` is
  a barrier.
- Not diff-friendly. Git diffs on `.db` files are meaningless. Git diffs on YAML
  task files are readable and useful.
- Backup complexity. Backing up an SQLite file mid-write can produce a corrupt
  backup. YAML files copy safely at any time with `cp -r`.
- Single point of failure. A corrupt `.db` file requires recovery tools. A
  corrupt YAML file is one bad file out of many — delete it and the system
  continues.

### Option B: Redis or another external database

Redis would give real-time pub/sub, atomic operations, and easy clustering.

**Why rejected:**
- Requires external infrastructure. Bernstein's core value proposition is
  `pip install bernstein && bernstein run` — zero external dependencies for a
  solo developer. Requiring Redis breaks this.
- State becomes invisible. You can't `ls` a Redis instance. Debugging requires
  `redis-cli` and knowledge of the key schema.
- Portability breaks. Moving a Bernstein project to another machine means
  migrating the Redis state. With files, `cp -r .sdd/` is the migration.

### Option C: Pure in-memory state

The initial prototype stored tasks in a Python dict in the task server process.

**Why rejected:**
- Lost on crash. Any process restart — even a clean `bernstein stop` + `bernstein
  run` — lost all task state.
- Not inspectable. You had to query the REST API to see what was happening.
- Not auditable. There was no record of what happened after the fact.

In production-like usage (the rag_challenge competition), losing in-progress task
state when an agent crashed caused significant rework. The JSONL checkpoint in
`runtime/tasks.jsonl` was added specifically to address this.

---

## Consequences

### Benefits

**Inspectable by default.** Any developer can understand the full system state
with standard tools:
```bash
ls .sdd/backlog/open/       # what's waiting
cat .sdd/backlog/claimed/*.yaml | grep "agent:"  # who's working on what
wc -l .sdd/metrics/tasks.jsonl  # how many tasks have completed
```

**Recoverable.** Copy `.sdd/backlog/` to another machine, run `bernstein run`,
and it resumes. No database migration. No checkpoint format to decode.

**Git-friendly.** Committing `.sdd/backlog/` gives you a git history of every
task ever created and completed alongside the code that resolved it. `git log
.sdd/backlog/done/` tells the story of the project.

**Zero infrastructure dependency.** `pip install bernstein` is the complete
installation. No database server to start, no connection string to configure.

**Debuggable.** When something goes wrong, the evidence is in plain text files.
`cat .sdd/backlog/failed/my-task.yaml` shows exactly what failed and why.

### Costs

**Not suitable for high-frequency writes.** Writing thousands of tasks per second
would be slow due to filesystem overhead. This is acceptable — Bernstein
orchestrates software development tasks that take minutes to hours, not
millisecond event streams.

**No atomic multi-task transactions.** Moving a task from `open/` to `claimed/`
is a file rename — atomic on most filesystems (POSIX `rename(2)`) but not across
network filesystems. For distributed multi-node use, the task server mediates
access via the REST API, which provides the serialization guarantee.

**Disk space grows over time.** `metrics/tasks.jsonl` is append-only. For
long-running projects, it accumulates indefinitely. This is intentional — metrics
are the audit trail. Pruning policy is left to the operator.

---

## Implementation

The task server (`core/server.py`) reads `.sdd/runtime/tasks.jsonl` at startup to
restore in-flight state. All mutations go through the REST API (`POST /tasks`,
`POST /tasks/{id}/complete`, etc.) which writes to the JSONL checkpoint
atomically.

Task files in `.sdd/backlog/` are the authoritative source of truth for task
definitions. The JSONL file is a runtime recovery cache. On conflict, the JSONL
takes precedence for status (claimed/done/failed), the YAML for definition
(goal, role, priority).

File locking (`core/file_locks.py`) prevents concurrent agents from claiming the
same task. The move from `open/` to `claimed/` is a `rename()` syscall —
atomic by POSIX guarantee.
