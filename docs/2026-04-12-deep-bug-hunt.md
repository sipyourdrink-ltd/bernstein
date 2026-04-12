# Deep Bug Hunt Report — 2026-04-12

10 parallel Opus agents audited ~24K lines of critical core code.
**109 raw findings, ~100 unique after deduplication.**

---

## CRITICAL (7 unique)

### C1. SAML signature validation completely skipped — full auth bypass
- **File:** `auth.py:950-982`
- **Agents:** #5, #10 (confirmed independently)
- **Bug:** `handle_saml_response()` parses SAML assertions but never validates the XML signature against the IdP certificate. Comment on line 958 explicitly acknowledges this. Any attacker who can POST to `/auth/saml/acs` can forge arbitrary SAML assertions, create admin accounts, and get valid JWTs.
- **Fix:** Implement XML signature verification using `xmlsec`/`signxml` against `idp_x509_cert`. Until then, disable SAML.

### C2. `_stop_spawning` flag set but never checked — budget overruns don't stop spawning
- **File:** `orchestrator.py:1785`
- **Agents:** #1, #9 (confirmed independently)
- **Bug:** Cost anomaly `stop_spawning` action sets `self._stop_spawning = True` but nothing in the codebase reads this attribute. Budget overruns have zero effect on agent spawning.
- **Fix:** Check `self._stop_spawning` in `claim_and_spawn_batches`.

### C3. Cost anomaly `kill_agent` signals always have `agent_id=None` — kills never execute
- **File:** `cost_anomaly.py:233, 266-273`
- **Agent:** #9
- **Bug:** `_check_per_task_ceiling` and `_check_token_ratio` emit signals without `agent_id`. The orchestrator handler checks `if signal.agent_id:` — always False. Runaway agents are never killed by cost anomaly detection.
- **Fix:** Propagate `agent_id` through `check_task_completion`, or resolve agent from `task_id` in handler.

### C4. `check_task_completion` defined but never called — per-task cost checks disconnected
- **File:** `cost_anomaly.py:147-167`
- **Agent:** #9
- **Bug:** The method that dispatches per-task ceiling, token ratio, and retry spiral checks is never invoked anywhere. Only `check_tick` (burn rate) and `check_spawn` are wired in.
- **Fix:** Call `check_task_completion` from the task completion path in the orchestrator.

### C5. `ProviderHealth.success_rate` resets to 0.0 after a single failure — blacklists healthy providers
- **File:** `router.py:212-217`
- **Agent:** #8
- **Bug:** `_recalculate_success_rate` uses consecutive counts, not rolling window. One failure after 99 successes → `success_rate=0.0`. Providers filtered by `min_health_score >= 0.7`, so a single failure blacklists any provider. All providers can be simultaneously blacklisted.
- **Fix:** Use rolling window or exponential moving average instead of consecutive counts.

### C6. `effective_max_agents()` mutates state on every call — double-decrements per tick
- **File:** `adaptive_parallelism.py:118-209`
- **Agent:** #8
- **Bug:** The method has side effects (modifying `_current_max`). Multiple calls per tick (status/logging + scheduling) cause compounding decrements. Parallelism oscillates unpredictably.
- **Fix:** Separate computation (`recompute()`) from read (`effective_max_agents()`).

### C7. `agent_test_mutation` missing from `VALID_GATE_NAMES` — crashes gate pipeline
- **File:** `gate_runner.py:36-61`
- **Agent:** #6
- **Bug:** Enabling `agent_test_mutation` raises `ValueError` that kills the entire gate run, prevents report persistence, and crashes the orchestrator tick.
- **Fix:** Add `"agent_test_mutation"` to `VALID_GATE_NAMES`.

---

## HIGH (25 unique)

### H1. SAML XML attributes misspelled — SSO completely non-functional
- **File:** `auth.py:886, 993, 995`
- **Agent:** #10
- **Bug:** Uses "Assertion" (single s) instead of "Assertion" (double s) in SAML XML attribute names. No IdP will accept these.

### H2. Path traversal in agent routes via `session_id`
- **File:** `routes/tasks.py:1344, 1363, 1376`
- **Agent:** #5
- **Bug:** `GET /agents/../../etc/passwd/logs` reads arbitrary files. `POST /agents/../../tmp/evil/kill` creates arbitrary files.
- **Fix:** Validate `session_id` matches `[a-zA-Z0-9_-]` or resolve+verify within `runtime_dir`.

### H3. Path traversal in `AuthStore` via user/session/device IDs
- **File:** `auth.py:537-538, 582-583, 645-646`
- **Agents:** #5, #10
- **Bug:** IDs interpolated directly into file paths. Forged JWT `sub` claim could read/write outside auth directory.
- **Fix:** Reject IDs containing `/`, `\`, `..`.

### H4. Auth bypass — inconsistent public path prefixes between middleware layers
- **File:** `server.py:85` vs `auth_middleware.py:74`
- **Agent:** #5
- **Bug:** `BearerAuthMiddleware` exempts `/export/` and `/dashboard/tasks/` but `SSOAuthMiddleware` doesn't. In legacy token mode, data exports are completely unauthenticated.

### H5. Shell injection via task title in completion curl commands
- **File:** `spawner.py:471-476`, `spawn_prompt.py:587-593`
- **Agent:** #2
- **Bug:** Task titles containing single quotes break out of shell strings in generated curl commands. Titles can come from external sources (GitHub issues, Slack).
- **Fix:** Escape quotes or use heredoc/file-based JSON payload.

### H6. `_task_to_record` drops 15+ critical fields — data loss on restart
- **File:** `task_store.py:587-622`
- **Agent:** #3
- **Bug:** Serialization omits `created_at`, `deadline`, `model`, `effort`, `retry_count`, `max_retries`, `metadata`, and more. After restart: retry_count resets to 0 (infinite retries), deadlines lost, model routing hints gone.

### H7. `update_task_priority` — no lock, no persistence, corrupts index
- **File:** `task_store.py:1695-1717`
- **Agent:** #3
- **Bug:** No async lock, no JSONL write, doesn't call `_index_remove` before `_index_add`. Creates duplicate priority heap entries; priority change lost on restart.

### H8. Worktree leak on spawn failure
- **File:** `spawner.py:1362-1370, 1697-1700`
- **Agent:** #2
- **Bug:** When adapter spawn fails after worktree creation, the worktree is never cleaned up. Repeated failures leak directories.

### H9. `_revoke_agent_token` never called — JWT tokens accumulate forever
- **File:** `spawner.py:903-919`
- **Agent:** #2
- **Bug:** Method exists but is never called in reap, kill, or cleanup paths. Token files and identity entries grow without bound.

### H10. Exit code 0 misclassified as "terminated by signal 0"
- **File:** `agent_lifecycle.py:143-153`
- **Agents:** #4, #9
- **Bug:** Clean exit falls through to signal detection branch. `abs(0)=0` matches no signal → `UNKNOWN, "process terminated by signal 0"`. Pollutes abort metrics for every cleanly exiting agent.

### H11. Orphaned task decisions made BEFORE partial work saved — data loss
- **File:** `agent_lifecycle.py:258-264`
- **Agent:** #4
- **Bug:** `handle_orphaned_task` runs before `_save_partial_work`. Agent's uncommitted changes aren't visible during the decision, so work is declared "no output" and task is failed.

### H12. Idle-recycled agents' tasks never retried/failed — stuck forever
- **File:** `agent_lifecycle.py:1666-1683`
- **Agent:** #4
- **Bug:** `_recycle_or_kill` kills process but never calls `handle_orphaned_task`. Tasks remain CLAIMED/IN_PROGRESS permanently.

### H13. Token monitor auto-kill orphans worktrees and tasks
- **File:** `token_monitor.py:689-693`
- **Agent:** #9
- **Bug:** `_handle_auto_kill` transitions agent to "dead" directly, preventing `refresh_agent_states` from ever running cleanup. Worktrees and tasks orphaned.

### H14. `asyncio.gather` without `return_exceptions=True` — one gate kills all gates
- **File:** `gate_runner.py:307`
- **Agent:** #6
- **Bug:** Any exception in any single gate cancels all other running gates. No partial results. No report persisted.

### H15. `detect_merge_conflicts` returns false-negative when merge-base fails
- **File:** `merge_queue.py:155-162`
- **Agent:** #6
- **Bug:** When `git merge-base` fails, function returns `has_conflicts=False`. Branch deleted? "No conflicts." Corrupted refs? "No conflicts."

### H16. Zombie processes from `shell=True` + timeout
- **File:** `quality_gates.py:493-508`
- **Agent:** #6
- **Bug:** `subprocess.run(shell=True, timeout=...)` kills the shell but not child processes. Timed-out pytest/pyright/mutmut processes run forever.

### H17. `_config.max_agents` corruption on exception — permanent throttle
- **File:** `orchestrator.py:1178, 1229`
- **Agent:** #1
- **Bug:** Config mutated before spawning, restored after. Any exception between = permanent reduction. Not in try/finally.

### H18. `_run_pytest` runs `pytest tests/` — leaks 100+ GB RAM
- **File:** `orchestrator.py:2776, 2883`
- **Agent:** #1
- **Bug:** Violates CLAUDE.md: "NEVER run `uv run pytest tests/ -x -q`". Should use `scripts/run_tests.py`.

### H19. BulletinBoard unbounded memory growth
- **File:** `bulletin.py:434-459`
- **Agent:** #7
- **Bug:** `_messages` list grows without bound. No eviction, no cap. Every `read_since()` iterates entire list under lock.

### H20. Bulletin/delegation/channel data lost on every restart
- **File:** `server.py:1391-1393`
- **Agent:** #7
- **Bug:** `flush_to_disk()`/`load_from_disk()` exist but are never called in production. All cross-agent state lost on restart.

### H21. `broadcast_message` skips file fallback for broken pipes
- **File:** `agent_ipc.py:87-105`
- **Agent:** #7
- **Bug:** When pipe breaks, session marked "failed" and file-based fallback skipped. Message lost entirely.

### H22. BulletinBoard deduplicates by float timestamp — drops messages
- **File:** `bulletin.py:602-628`
- **Agent:** #7
- **Bug:** Two messages with same `time.time()` value → one silently dropped on reload.

### H23. Plan loader silently drops forward-referenced stage dependencies
- **File:** `plan_loader.py:222-226`
- **Agent:** #8
- **Bug:** `depends_on: [later_stage]` silently dropped if stage appears later in YAML. Tasks run in parallel when they should be sequential.

### H24. Plan loader silently loses tasks from duplicate stage names
- **File:** `plan_loader.py:208`
- **Agent:** #8
- **Bug:** Duplicate stage names overwrite task lists. Downstream `depends_on` only sees last stage's tasks.

### H25. MetricsCollector unbounded memory growth
- **File:** `metric_collector.py:260-262`
- **Agent:** #10
- **Bug:** `_task_metrics` and `_agent_metrics` never evicted. Linear memory growth with task count.

---

## MEDIUM (35 unique)

### M1. `recover_stale_claimed_tasks` bypasses FSM, no JSONL persistence (Agents #3, #5)
### M2. `PENDING_APPROVAL` status has zero transitions — dead state (Agent #3)
### M3. `session.files_changed` AttributeError silently kills A/B recording (Agent #3)
### M4. Retry task `created_at` set to future — backoff delay does nothing (Agent #3)
### M5. `_complete_parent_if_ready` ignores FAILED subtasks — parent stuck forever (Agent #3)
### M6. `claim_by_id` silently returns already-claimed tasks (Agent #3)
### M7. `_finalize_trace` skipped for subprocess agents — traces leak (Agent #2)
### M8. `isinstance(bridge_status, object)` always True — no-op guard, crash on None (Agent #2)
### M9. Timeout watchdog timers never cancelled on reap — stale PID kill (Agent #2)
### M10. Log FD closed before `_probe_fast_exit` in 14 adapters (Agent #2)
### M11. `system_addendum` always empty — feature dead on arrival (Agent #2)
### M12. `_reap_completed_agent` missing task/file/session cleanup (Agent #4)
### M13. SIGKILL always classified as OOM even for orchestrator kills (Agent #4)
### M14. IPC `_stdin_pipes` no thread safety (Agents #4, #7)
### M15. Heartbeat-based timeout extension allows infinite extension (Agent #4)
### M16. Suppressed kill exceptions leave agent in non-dead state (Agent #4)
### M17. `_persist_lines_changed` TOCTOU race (Agent #5)
### M18. `heartbeat()`/`mark_stale_dead()` no locking on `_agents` dict (Agent #5)
### M19. Access log rotation race — can lose `.1` file contents (Agent #5)
### M20. `TaskNotificationManager` unbounded subscriber queues (Agent #5)
### M21. AbortChain cleanup orphans grandchildren's `_parent_of` entries (Agent #9)
### M22. Circuit breaker falsely reset between 80-90% utilization (Agent #9)
### M23. Cost anomaly tracking dicts grow unboundedly (Agent #9)
### M24. MessageBoard delegations never removed from memory (Agent #7)
### M25. `flush_to_disk` writes ALL messages every call — exponential JSONL growth (Agent #7)
### M26. GitLab job event parsed with wrong payload structure (Agent #7)
### M27. SSEBus no thread safety on `_subscribers` (Agent #7)
### M28. Fallback tier ordering skips FREE tier (Agent #8)
### M29. Substring model matching produces false positives (Agent #8)
### M30. Plan loader no circular dependency detection (Agent #8)
### M31. `RunCostReport.to_dict()` drops token-level fields (Agent #10)
### M32. Container log streaming leaks file handles (Agent #10)
### M33. Concurrent metrics dict iteration unsafe (Agent #10)
### M34. `MergeQueue.enqueue` no deduplication (Agent #6)
### M35. `merge-tree` exit code not checked — second false-negative path (Agent #6)
### M36. `BERNSTEIN_SKIP_GATES` + `allow_bypass=False` crashes pipeline (Agent #6)
### M37. Gate cache grows unboundedly (Agent #6)
### M38. Non-atomic cache write — crash corrupts entire cache (Agent #6)
### M39. `_evolve_run_tests` always returns zeros — governance thinks tests fail (Agent #1)
### M40. Health check early return skips agent reaping (Agent #1)
### M41. 7 per-task tracking dicts never cleaned up (Agent #1)
### M42. `_check_task_deadlines` fires repeatedly — notification spam (Agent #1)
### M43. Double-count server failures across health check and fetch (Agent #1)
### M44. `_pending_ruff_future` stuck forever on JSONDecodeError (Agent #1)

---

## LOW (19 unique)

### L1. Module-level caches not thread-safe (Agent #2)
### L2. Container spawn uses deprecated flag (Agent #2)
### L3. IPC broken pipe leaks FD (Agents #4, #7)
### L4. Double heartbeat read TOCTOU (Agent #4)
### L5. 404 detection via string matching (Agent #4)
### L6. `_runtime_cache` global mutation (Agent #5)
### L7. `sdd_dir` variable rebinding (Agent #5)
### L8. IP allowlist missing prefix check (Agent #5)
### L9. `status_summary` references nonexistent `task.cost_usd` (Agent #3)
### L10. `add_snapshot` no lock (Agent #3)
### L11. Archive JSONL grows unbounded (Agent #3)
### L12. Trace parser timestamp formula (Agent #10)
### L13. JWT expiry sentinel fragility (Agent #10)
### L14. Metric file date timezone mismatch (Agent #10)
### L15. Global collector singleton not resettable (Agent #10)
### L16. TOCTOU in bulletin load_from_disk (Agent #7)
### L17. Semantic cache no atomic writes (Agent #7)
### L18. DirectChannel resolved queries accumulate (Agent #7)
### L19. Round-robin agent index skips on list resize (Agent #8)
### L20. TokenEscalationTracker list grows unbounded (Agent #8)
### L21. Janitor judge retry regex can match stale markers (Agent #9)
### L22. `recycle_idle_agents` called twice per tick (Agent #1)
### L23. httpx.Client never closed on shutdown (Agent #1)
### L24. Replay log records total cost instead of per-task (Agent #1)
### L25. Dead-code gate overrides `required=False` (Agent #6)

---

## Summary by severity

| Severity | Count |
|----------|-------|
| Critical | 7 |
| High | 25 |
| Medium | 44 |
| Low | 25 |
| **Total** | **~101 unique** |

## Top 10 most impactful (fix first)

1. **C1** — SAML auth bypass (security)
2. **H2/H3** — Path traversal (security)
3. **H5** — Shell injection via task titles (security)
4. **H4** — Auth bypass via inconsistent middleware (security)
5. **C2+C3+C4** — Cost controls are completely disconnected (budget)
6. **C5** — Single failure blacklists healthy providers (availability)
7. **H6** — Task data loss on restart (reliability)
8. **H11+H12** — Work loss and stuck tasks on agent death (reliability)
9. **H18** — `pytest tests/` leaks 100GB RAM (operational)
10. **C6+H17** — Parallelism corruption (operational)
