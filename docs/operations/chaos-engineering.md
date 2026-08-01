# Chaos engineering

`bernstein chaos` is a small fault-injection toolkit for SREs who want to
prove the orchestrator survives the failure modes its docs claim it
survives. It is **not** a load generator and **not** a security fuzzer.
The verbs map directly to scenarios the deterministic core is supposed
to recover from: an agent dies mid-task, a file disappears from a
worktree.

The CLI lives in `cli/commands/chaos_cmd.py:31` (`@click.group("chaos")`).
All state - including replayable history - is written under
`.sdd/runtime/chaos/` (`chaos_cmd.py:28`).

---

## Why chaos testing for an agent orchestrator

A multi-agent orchestrator looks deterministic on a green run, but its
failure paths are exercised rarely. The same boring path covers:

- **WAL replay**: an agent crashes mid-task. The orchestrator must
  re-claim, re-spawn, and finish the work without producing two
  conflicting commits (see `architecture/state-persistence.md`).
- **Worktree integrity**: a file the agent was editing disappears
  underneath it. The agent must surface a clean error rather than
  silently rewriting it.
- **SLO discipline**: error-budget burn from one of the above must move
  the dashboard from green → yellow → red and trigger remediation.

Running these scenarios before they show up in production is the
fastest way to catch regressions in any of the recovery paths above.

---

## `bernstein chaos` group

Every subcommand records an entry into
`.sdd/runtime/chaos/chaos_log.jsonl` so that runs can be replayed and
correlated against orchestrator logs.

### `agent-kill` - kill an active agent process

```
bernstein chaos agent-kill [--agent-id <name>]
```

Walks `.sdd/runtime/agents/`, finds every agent whose `pid` file points
at a live process, and sends `SIGTERM` to one of them
(`chaos_cmd.py:73-98`). With `--agent-id` you target a specific agent;
without it, one is chosen at random.

**What recovery should look like.** The orchestrator detects the dead
PID via heartbeat, marks the task as failed, replays the WAL, and either
re-claims the task on a fresh agent or, if the bandit cascade decides
the tier is too flaky, escalates to the next adapter. No commit should
land for the killed run, and no second commit should land for the same
task ID once it completes.

### `file-remove` - yank a file out of a worktree

```
bernstein chaos file-remove [--pattern "*.py"]
```

Picks a random non-`__init__.py` file under
`.claude/worktrees/*/src/**/<pattern>`, copies it to a `.chaos_backup`
sibling, and deletes the original (`chaos_cmd.py:101-136`).

**What recovery should look like.** The agent operating in that
worktree must either fail loudly (gate failure, missing import) or
re-fetch the file from the merge base. The backup is left in place so
post-mortems can verify the original content.

### `status` - replay the chaos log

```
bernstein chaos status [--limit 20]
```

Reads `.sdd/runtime/chaos/chaos_log.jsonl` and prints a table of recent
events: timestamp, scenario, target, success/error
(`chaos_cmd.py:139-175`).

### `slo` - read the SLO dashboard during the experiment

```
bernstein chaos slo
```

Loads `.sdd/metrics/slos.json` and prints traffic-light status per SLO
plus the error-budget panel (`chaos_cmd.py:178-232`).

The output table contains:

- `target` (e.g. `99%`) - the SLO threshold.
- `current` (e.g. `97.4%`) - the live measurement.
- `status` - `GREEN` / `YELLOW` / `RED`.

The error-budget panel reports `total_tasks`, `failed_tasks`, and
`budget_remaining` / `budget_total`. A non-empty `actions` list at the
bottom indicates remediation already triggered automatically (for
example, lower `max_agents`).

---

## Reading SLO impact during a chaos run

The intended ops loop:

1. Note the current `bernstein chaos slo` baseline. All SLOs should be
   `GREEN` and the error budget should not be near zero.
2. Inject: `bernstein chaos agent-kill`.  
3. Watch `bernstein chaos slo` and `bernstein status` while the
   orchestrator recovers.
4. Confirm:
   - SLOs trend toward `RED` only as far as the documented blast radius.
   - The error budget loses ≤ the cost of one task.
   - `bernstein chaos status` shows the injected event recorded.
5. After recovery, SLOs should return to `GREEN` without manual
   intervention. If they do not, that is a recovery bug, not a chaos
   tooling bug.

For the wider observability picture (Prometheus, Grafana, anomaly
detection) see `operations/observability-overview.md`. The chaos CLI
deliberately exposes only the slice an SRE needs while the experiment
is in flight.

---

## Safety rails - what is never injected

The chaos CLI is intentionally narrow:

- **No user data is touched.** `file-remove` operates on
  `.claude/worktrees/*/src/**` only. It will not delete files outside
  the worktree, and it always writes a `.chaos_backup` sibling first
  (`chaos_cmd.py:126-130`).
- **No production credentials are exfiltrated or rotated.** No
  subcommand reads from the credential vault.
- **No commits or PRs are produced.** The CLI never invokes git or
  GitHub.
- **`agent-kill` uses `SIGTERM`, not `SIGKILL`.** The agent gets a
  chance to flush; if it ignores the signal, an external `SIGKILL` is
  the operator's responsibility.
- **No chaos commands run inside `bernstein run`.** They are operator
  tools, invoked manually. There is no scheduler that injects faults
  during a real customer run.

If you need a fault that the CLI does not expose, prefer extending
`chaos_cmd.py` with a new subcommand over hand-editing `.sdd/runtime/`
state directly - the audit trail in `chaos_log.jsonl` is what makes a
chaos run reproducible.

---

## Code pointers

- `cli/commands/chaos_cmd.py:31` - `@click.group("chaos")` entry point.
- `cli/commands/chaos_cmd.py:36-70` - active-agent discovery and target
  selection.
- `cli/commands/chaos_cmd.py:73-98` - `agent-kill`.
- `cli/commands/chaos_cmd.py:101-136` - `file-remove` with backup.
- `cli/commands/chaos_cmd.py:139-175` - `status` (chaos log table).
- `cli/commands/chaos_cmd.py:178-232` - `slo` (SLO dashboard).
- `cli/commands/chaos_cmd.py:235-253` - `_record_chaos_event` (JSONL
  append).
- `.sdd/runtime/chaos/chaos_log.jsonl` - replayable event log.
- `.sdd/metrics/slos.json` - SLO dashboard source consumed by
  `bernstein chaos slo`.
