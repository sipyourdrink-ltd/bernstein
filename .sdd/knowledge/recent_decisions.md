# Recent Decisions

No decisions recorded yet.

## [2026-03-29 00:05] [RETRY 1] 347b — Environment Variable Isolation for Agents (030b21aa1b74)
Completed: [RETRY 1] 347b — Environment Variable Isolation for Agents. env_isolation.py fully implemented with build_filtered_env(), used by all 7 adapters (claude, codex, gemini, aider, amp, qwen, generic, manager). 17 unit tests pass.

## [2026-03-29 00:06] [DECOMPOSE] [RETRY 2] [RETRY 1] 334b — Real-Time Cost Dashboard with Per-Agent Tracking (d9bc8c377f54)
Completed: [DECOMPOSE] [RETRY 2] [RETRY 1] 334b — Real-Time Cost Dashboard with Per-Agent Tracking. Created 5 atomic subtasks: 334b-01 (cost API), 334b-02 (web dashboard), 334b-03 (TUI widget), 334b-04 (history+alerts), 334b-05 (cost model).

## [2026-03-29 00:06] 334d-02: Task Timeline & Gantt Visualization (7b529ad5fd20)
Completed: 334d-02: Task Timeline & Gantt Visualization

## [2026-03-29 00:07] 383 — Creative evolution: visionary → analyst → production pipeline (5a0b9afcdaab)
Completed: 383 — Creative evolution: visionary → analyst → production pipeline. All components already implemented: VisionaryProposal, AnalystVerdict, CreativePipeline classes in creative.py (22KB), visionary and analyst role templates in templates/roles/, creative_vision in evolution loop focus rotation. 20/20 tests passing.

## [2026-03-29 00:07] 334b-01: Cost aggregation API endpoints (8ed3bab37d2c)
Completed: 334b-01: Cost aggregation API endpoints

## [2026-03-29 00:09] 333c — Cross-Run Project Memory (8f73aab019e7)
Completed: 333c — Cross-Run Project Memory

## [2026-03-29 00:09] [RETRY 1] 333d — Smart Task Distribution (No Greedy Claiming) (e9d0188b9b5e)
Completed: 333d — Smart Task Distribution. The core implementation (per-role cap via ceil(max_agents*role_tasks/total_tasks), starvation prevention, round-robin grouping) was already in place across tick_pipeline.py and task_lifecycle.py. Added 3 new integration tests in TestPerRoleCapDistribution to explicitly verify: (1) backend at cap does not steal remaining slots from other roles, (2) proportional cap allows both roles to get agents, (3) ceil() ensures minimum 1 slot per role.

## [2026-03-29 00:09] 334b-04: Cost history & alert thresholds (af84998a6d2f)
Completed: 334b-04: Cost history & alert thresholds

## [2026-03-29 00:10] [DECOMPOSE] [RETRY 2] [RETRY 1] 332 — Zero-Config Agent Setup (2e2176216934)
Decomposed task 039847068f48 into 4 atomic subtasks: 332a (agent discovery auth), 332b (bootstrap auto-config), 332c (zero-config entry point), 332d (tests + CLI overrides). Each subtask targets specific files with clear completion signals.

## [2026-03-29 00:10] 332b — Bootstrap Auto-config: Generate bernstein.yaml from detected agents (413e8aa4594d)
[fast-path] ruff format: 1 file(s) reformatted in 0.1s

## [2026-03-29 00:11] 334b-05: Cost calculation model & projection (5ebb70bd3a4b)
Completed: 334b-05: Cost calculation model & projection

## [2026-03-29 00:11] [DECOMPOSE] [RETRY 2] [RETRY 1] 333d — Smart Task Distribution (No Greedy Claiming) (f2e59b41d446)
Completed: [DECOMPOSE] 333d — Smart Task Distribution decomposed into 5 atomic subtasks (333d-01 through 333d-05). Each targets specific files with clear completion signals. Ready for parallel execution.

## [2026-03-29 00:12] 334d-03: Live Logs & File Lock Visualization (63619a1e5614)
Completed: 334d-03: Live Logs & File Lock Visualization

## [2026-03-29 00:12] 373 — Evaluation harness: multiplicative scoring, LLM judge, failure taxonomy (3b82a0314003)
Completed: 373 — Evaluation harness: multiplicative scoring, LLM judge, failure taxonomy

## [2026-03-29 00:15] 332a — Agent Discovery: Auto-detect and authenticate (e739aa17cadd)
Completed: 332a — Agent Discovery: Auto-detect and authenticate. Implemented detect_auth_status() function that returns dict mapping agent_name to (installed: bool, authenticated: bool). Added _detect_aider() for Aider agent detection. All 42 unit tests pass, including 3 new tests for detect_auth_status() and 4 tests for _detect_aider().
