# Metrics: Schema registry for .sdd/metrics streams

Declared a schema registry for all 31 metric streams under `.sdd/metrics/*.jsonl`. Each stream now has a versioned schema with field types, cardinality notes, and owning writer. The registry enables validation of metric records against declared schemas, detecting undeclared fields and missing required fields.

Writers will stamp `schema_version` in future work (issue #5442 slice 2+). This slice provides the foundation for typed metric export, named queries, and MCP tool integration.

**What changed:**
- New module `src/bernstein/core/observability/schema_registry.py` declaring all 31 streams
- Registry validation functions `get_schema()` and `validate_record()`
- Guard test ensuring registry is complete and can detect schema violations

**Streams registered:** tasks, evaluations, test_runs, cost, cost_history, quality_gates, review_rubric, routing_decisions, tool_timing, tool_audit, anomalies, guardrails, rule_violations, policy_violations, security_incidents, kill_audit, cache_breaks, graduation, effectiveness, quality_scores, verification_nudges, ab_test_results, evolution_weights, evolve_cycles, governance_log, file_health, file_health_touches, command_policy, calibration, swe_bench_results, programbench_results.
