# Recent Decisions

No decisions recorded yet.

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

## [2026-03-29 00:16] 332c — Zero-config entry point: Skip init, auto-initialize .sdd/ (e17788733ff0)
Completed: 332c — Zero-config entry point: Skip init, auto-initialize .sdd/

## [2026-03-29 00:17] 332d — Zero-config tests and CLI overrides (--cli, --model) (c92c7af40f8b)
Completed: 332d — Zero-config tests and CLI overrides (--cli, --model). Added 7 new test cases, implemented --cli flag (auto/claude/codex/gemini/aider/qwen), implemented --model flag, both override auto-detected and config-file values. 20/20 tests passing.

## [2026-03-29 00:18] 392 — Orchestration Benchmark (928a170c045a)
Completed: 392 — Orchestration Benchmark. Added --issues-file mode to run_benchmark.py for 25 real GitHub issues. Statistical testing via two-proportion z-test, Wilson CIs, Cohen's h, bootstrap CIs. Results published in benchmarks/results/. README updated with Benchmark 2 section. All 64 benchmark tests pass.

## [2026-03-29 00:18] [DECOMPOSE] 341 — SWE-Bench Evaluation with Scaffolding Thesis (fcdae44c7d1d)
Completed: [DECOMPOSE] 341 — SWE-Bench Evaluation with Scaffolding Thesis. Created 4 atomic subtasks: 341-01 (harness setup), 341-02 (baselines), 341-03 (Bernstein multi-agent), 341-04 (analysis & narrative).

## [2026-03-29 00:19] [RETRY 1] 333d-01: Role-locked task claiming in orchestrator (0908013d4b21)
Completed: [RETRY 1] 333d-01: Role-locked task claiming in orchestrator. Both claim_by_id() and claim_batch() already enforce role matching via agent_role parameter. All 5 unit tests pass.

## [2026-03-29 00:19] 374 — Eval-gated evolution: only apply changes that improve eval scores (f1a03d7e4113)
Completed: 374 — Eval-gated evolution: only apply changes that improve eval scores. All components already implemented: EvalGate class in gate.py with L0-L3 tiered eval, baseline tracking via EvalBaseline, trajectory logging to eval_trajectory.jsonl, convenience eval_gate() function, and full integration in evolution loop step 7b. 30/30 tests passing.

## [2026-03-29 00:19] 334d-04: Cost Burn & Performance Charts (b859593c62c1)
Completed: 334d-04: Cost Burn & Performance Charts

## [2026-03-29 00:20] [RETRY 1] 333d-02: Fair spawn distribution algorithm (6a052b841201)
Completed: [RETRY 1] 333d-02: Fair spawn distribution algorithm. The per-role cap (ceil(max_agents * role_tasks / total_tasks)) and starvation prevention were already implemented in task_lifecycle.py and tick_pipeline.py. Added test_qa_gets_slot_before_backend_third to TestPerRoleCapDistribution: verifies that with backend=5 tasks, 2 alive agents (at cap=2) and qa=3 tasks, 0 alive agents, the 1 remaining global slot goes to qa not backend. All 4 tests pass.
