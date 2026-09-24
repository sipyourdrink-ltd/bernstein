"""Schema registry for .sdd/metrics/*.jsonl streams.

Every metric stream declares its schema here: name, version, fields with types,
cardinality, owning writer. Writers stamp schema_version in every record.
A guard test fails when a writer emits an undeclared field.

Schema version: v1 (initial registry snapshot as of 2026-09-20)
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class FieldType(Enum):
    """Field types for metric schema declarations."""

    STRING = "string"
    INT = "int"
    FLOAT = "float"
    BOOL = "bool"
    DICT = "dict"
    LIST = "list"
    TIMESTAMP = "timestamp"


class Cardinality(Enum):
    """Cardinality notes for metric streams."""

    ONE_PER_TASK = "one_per_task"
    MANY_PER_TASK = "many_per_task"
    ONE_PER_RUN = "one_per_run"
    MANY_PER_RUN = "many_per_run"
    ONE_PER_EVENT = "one_per_event"


@dataclass(frozen=True)
class FieldSpec:
    """Field specification in a metric schema."""

    name: str
    type: FieldType
    required: bool = True
    description: str = ""


@dataclass(frozen=True)
class StreamSchema:
    """Schema for a single metric stream."""

    name: str
    version: str
    fields: tuple[FieldSpec, ...]
    cardinality: Cardinality
    owner: str
    description: str = ""


# Registry of all .sdd/metrics/*.jsonl streams
SCHEMAS: dict[str, StreamSchema] = {
    "tasks": StreamSchema(
        name="tasks",
        version="v1",
        fields=(
            FieldSpec("task_id", FieldType.STRING),
            FieldSpec("role", FieldType.STRING),
            FieldSpec("status", FieldType.STRING),
            FieldSpec("model", FieldType.STRING, required=False),
            FieldSpec("provider", FieldType.STRING, required=False),
            FieldSpec("start_time", FieldType.TIMESTAMP),
            FieldSpec("end_time", FieldType.TIMESTAMP, required=False),
            FieldSpec("cost_usd", FieldType.FLOAT, required=False),
            FieldSpec("retry_count", FieldType.INT, required=False),
            FieldSpec("tokens_prompt", FieldType.INT, required=False),
            FieldSpec("tokens_completion", FieldType.INT, required=False),
            FieldSpec("schema_version", FieldType.STRING),
        ),
        cardinality=Cardinality.ONE_PER_TASK,
        owner="task_lifecycle.py",
        description="Per-task completion records",
    ),
    "evaluations": StreamSchema(
        name="evaluations",
        version="v1",
        fields=(
            FieldSpec("task_id", FieldType.STRING),
            FieldSpec("model", FieldType.STRING),
            FieldSpec("role", FieldType.STRING),
            FieldSpec("complexity", FieldType.STRING),
            FieldSpec("result", FieldType.STRING),
            FieldSpec("duration_s", FieldType.FLOAT),
            FieldSpec("cost_usd", FieldType.FLOAT),
            FieldSpec("step_count", FieldType.INT),
            FieldSpec("quality_gate_results", FieldType.DICT),
            FieldSpec("timestamp", FieldType.TIMESTAMP),
            FieldSpec("schema_version", FieldType.STRING),
        ),
        cardinality=Cardinality.ONE_PER_TASK,
        owner="eval_store.py",
        description="Evaluation framework records",
    ),
    "test_runs": StreamSchema(
        name="test_runs",
        version="v1",
        fields=(
            FieldSpec("task_id", FieldType.STRING),
            FieldSpec("test_name", FieldType.STRING),
            FieldSpec("passed", FieldType.BOOL),
            FieldSpec("duration_s", FieldType.FLOAT),
            FieldSpec("timestamp", FieldType.TIMESTAMP),
            FieldSpec("schema_version", FieldType.STRING),
        ),
        cardinality=Cardinality.MANY_PER_TASK,
        owner="quality/test_runs.py",
        description="Test execution results",
    ),
    "cost": StreamSchema(
        name="cost",
        version="v1",
        fields=(
            FieldSpec("task_id", FieldType.STRING),
            FieldSpec("model", FieldType.STRING),
            FieldSpec("provider", FieldType.STRING),
            FieldSpec("tokens_prompt", FieldType.INT),
            FieldSpec("tokens_completion", FieldType.INT),
            FieldSpec("cost_usd", FieldType.FLOAT),
            FieldSpec("timestamp", FieldType.TIMESTAMP),
            FieldSpec("schema_version", FieldType.STRING),
        ),
        cardinality=Cardinality.MANY_PER_TASK,
        owner="cost/cost_tracker.py",
        description="Per-call cost records",
    ),
    "cost_history": StreamSchema(
        name="cost_history",
        version="v1",
        fields=(
            FieldSpec("date", FieldType.STRING),
            FieldSpec("total_cost_usd", FieldType.FLOAT),
            FieldSpec("task_count", FieldType.INT),
            FieldSpec("timestamp", FieldType.TIMESTAMP),
            FieldSpec("schema_version", FieldType.STRING),
        ),
        cardinality=Cardinality.ONE_PER_RUN,
        owner="cost/cost_history.py",
        description="Daily cost snapshots",
    ),
    "quality_gates": StreamSchema(
        name="quality_gates",
        version="v1",
        fields=(
            FieldSpec("task_id", FieldType.STRING),
            FieldSpec("gate", FieldType.STRING),
            FieldSpec("passed", FieldType.BOOL),
            FieldSpec("blocked", FieldType.BOOL),
            FieldSpec("detail", FieldType.STRING, required=False),
            FieldSpec("timestamp", FieldType.TIMESTAMP),
            FieldSpec("schema_version", FieldType.STRING),
        ),
        cardinality=Cardinality.MANY_PER_TASK,
        owner="quality/quality_gates.py",
        description="Quality gate check results",
    ),
    "review_rubric": StreamSchema(
        name="review_rubric",
        version="v1",
        fields=(
            FieldSpec("task_id", FieldType.STRING),
            FieldSpec("score", FieldType.FLOAT),
            FieldSpec("feedback", FieldType.STRING, required=False),
            FieldSpec("timestamp", FieldType.TIMESTAMP),
            FieldSpec("schema_version", FieldType.STRING),
        ),
        cardinality=Cardinality.ONE_PER_TASK,
        owner="quality/review_rubric.py",
        description="Review rubric scores",
    ),
    "routing_decisions": StreamSchema(
        name="routing_decisions",
        version="v1",
        fields=(
            FieldSpec("task_id", FieldType.STRING),
            FieldSpec("model_chosen", FieldType.STRING),
            FieldSpec("reason", FieldType.STRING),
            FieldSpec("timestamp", FieldType.TIMESTAMP),
            FieldSpec("schema_version", FieldType.STRING),
        ),
        cardinality=Cardinality.ONE_PER_TASK,
        owner="routing/router_core.py",
        description="Model routing decisions",
    ),
    "tool_timing": StreamSchema(
        name="tool_timing",
        version="v1",
        fields=(
            FieldSpec("session_id", FieldType.STRING),
            FieldSpec("tool_name", FieldType.STRING),
            FieldSpec("duration_ms", FieldType.FLOAT),
            FieldSpec("timestamp", FieldType.TIMESTAMP),
            FieldSpec("schema_version", FieldType.STRING),
        ),
        cardinality=Cardinality.MANY_PER_TASK,
        owner="observability/tool_timing.py",
        description="Tool call timing records",
    ),
    "tool_audit": StreamSchema(
        name="tool_audit",
        version="v1",
        fields=(
            FieldSpec("session_id", FieldType.STRING),
            FieldSpec("tool_name", FieldType.STRING),
            FieldSpec("allowed", FieldType.BOOL),
            FieldSpec("reason", FieldType.STRING, required=False),
            FieldSpec("timestamp", FieldType.TIMESTAMP),
            FieldSpec("schema_version", FieldType.STRING),
        ),
        cardinality=Cardinality.MANY_PER_TASK,
        owner="security/command_policy.py",
        description="Tool authorization audit",
    ),
    "anomalies": StreamSchema(
        name="anomalies",
        version="v1",
        fields=(
            FieldSpec("signal_type", FieldType.STRING),
            FieldSpec("severity", FieldType.STRING),
            FieldSpec("description", FieldType.STRING),
            FieldSpec("timestamp", FieldType.TIMESTAMP),
            FieldSpec("schema_version", FieldType.STRING),
        ),
        cardinality=Cardinality.MANY_PER_RUN,
        owner="observability/behavior_anomaly.py",
        description="Behavior anomaly signals",
    ),
    "guardrails": StreamSchema(
        name="guardrails",
        version="v1",
        fields=(
            FieldSpec("event_type", FieldType.STRING),
            FieldSpec("severity", FieldType.STRING),
            FieldSpec("detail", FieldType.STRING),
            FieldSpec("timestamp", FieldType.TIMESTAMP),
            FieldSpec("schema_version", FieldType.STRING),
        ),
        cardinality=Cardinality.MANY_PER_RUN,
        owner="security/guardrails.py",
        description="Guardrail violation events",
    ),
    "rule_violations": StreamSchema(
        name="rule_violations",
        version="v1",
        fields=(
            FieldSpec("task_id", FieldType.STRING),
            FieldSpec("rule", FieldType.STRING),
            FieldSpec("severity", FieldType.STRING),
            FieldSpec("message", FieldType.STRING),
            FieldSpec("timestamp", FieldType.TIMESTAMP),
            FieldSpec("schema_version", FieldType.STRING),
        ),
        cardinality=Cardinality.MANY_PER_TASK,
        owner="security/rule_enforcer.py",
        description="Rule enforcement violations",
    ),
    "policy_violations": StreamSchema(
        name="policy_violations",
        version="v1",
        fields=(
            FieldSpec("session_id", FieldType.STRING),
            FieldSpec("policy", FieldType.STRING),
            FieldSpec("detail", FieldType.STRING),
            FieldSpec("timestamp", FieldType.TIMESTAMP),
            FieldSpec("schema_version", FieldType.STRING),
        ),
        cardinality=Cardinality.MANY_PER_TASK,
        owner="security/policy_engine.py",
        description="Policy violation records",
    ),
    "security_incidents": StreamSchema(
        name="security_incidents",
        version="v1",
        fields=(
            FieldSpec("incident_type", FieldType.STRING),
            FieldSpec("severity", FieldType.STRING),
            FieldSpec("detail", FieldType.STRING),
            FieldSpec("timestamp", FieldType.TIMESTAMP),
            FieldSpec("schema_version", FieldType.STRING),
        ),
        cardinality=Cardinality.ONE_PER_EVENT,
        owner="security/security_incident_response.py",
        description="Security incident records",
    ),
    "kill_audit": StreamSchema(
        name="kill_audit",
        version="v1",
        fields=(
            FieldSpec("session_id", FieldType.STRING),
            FieldSpec("reason", FieldType.STRING),
            FieldSpec("timestamp", FieldType.TIMESTAMP),
            FieldSpec("schema_version", FieldType.STRING),
        ),
        cardinality=Cardinality.ONE_PER_EVENT,
        owner="security/kill_audit.py",
        description="Agent termination audit",
    ),
    "cache_breaks": StreamSchema(
        name="cache_breaks",
        version="v1",
        fields=(
            FieldSpec("cache_key", FieldType.STRING),
            FieldSpec("reason", FieldType.STRING),
            FieldSpec("timestamp", FieldType.TIMESTAMP),
            FieldSpec("schema_version", FieldType.STRING),
        ),
        cardinality=Cardinality.MANY_PER_RUN,
        owner="adapters/caching_adapter.py",
        description="Cache invalidation events",
    ),
    "graduation": StreamSchema(
        name="graduation",
        version="v1",
        fields=(
            FieldSpec("task_id", FieldType.STRING),
            FieldSpec("from_stage", FieldType.STRING),
            FieldSpec("to_stage", FieldType.STRING),
            FieldSpec("timestamp", FieldType.TIMESTAMP),
            FieldSpec("schema_version", FieldType.STRING),
        ),
        cardinality=Cardinality.MANY_PER_TASK,
        owner="quality/graduation.py",
        description="Graduation stage transitions",
    ),
    "effectiveness": StreamSchema(
        name="effectiveness",
        version="v1",
        fields=(
            FieldSpec("task_id", FieldType.STRING),
            FieldSpec("effectiveness_score", FieldType.FLOAT),
            FieldSpec("timestamp", FieldType.TIMESTAMP),
            FieldSpec("schema_version", FieldType.STRING),
        ),
        cardinality=Cardinality.ONE_PER_TASK,
        owner="quality/effectiveness.py",
        description="Task effectiveness scores",
    ),
    "quality_scores": StreamSchema(
        name="quality_scores",
        version="v1",
        fields=(
            FieldSpec("task_id", FieldType.STRING),
            FieldSpec("quality_score", FieldType.FLOAT),
            FieldSpec("timestamp", FieldType.TIMESTAMP),
            FieldSpec("schema_version", FieldType.STRING),
        ),
        cardinality=Cardinality.ONE_PER_TASK,
        owner="quality/quality_score.py",
        description="Task quality scores",
    ),
    "verification_nudges": StreamSchema(
        name="verification_nudges",
        version="v1",
        fields=(
            FieldSpec("task_id", FieldType.STRING),
            FieldSpec("nudge_type", FieldType.STRING),
            FieldSpec("detail", FieldType.STRING, required=False),
            FieldSpec("timestamp", FieldType.TIMESTAMP),
            FieldSpec("schema_version", FieldType.STRING),
        ),
        cardinality=Cardinality.MANY_PER_TASK,
        owner="quality/verification_nudge.py",
        description="Verification nudge events",
    ),
    "ab_test_results": StreamSchema(
        name="ab_test_results",
        version="v1",
        fields=(
            FieldSpec("experiment_id", FieldType.STRING),
            FieldSpec("variant", FieldType.STRING),
            FieldSpec("metric", FieldType.STRING),
            FieldSpec("value", FieldType.FLOAT),
            FieldSpec("timestamp", FieldType.TIMESTAMP),
            FieldSpec("schema_version", FieldType.STRING),
        ),
        cardinality=Cardinality.MANY_PER_RUN,
        owner="quality/ab_test_results.py",
        description="A/B test results",
    ),
    "evolution_weights": StreamSchema(
        name="evolution_weights",
        version="v1",
        fields=(
            FieldSpec("cycle_id", FieldType.STRING),
            FieldSpec("weights", FieldType.DICT),
            FieldSpec("timestamp", FieldType.TIMESTAMP),
            FieldSpec("schema_version", FieldType.STRING),
        ),
        cardinality=Cardinality.ONE_PER_EVENT,
        owner="evolution/aggregator.py",
        description="Evolution weight history",
    ),
    "evolve_cycles": StreamSchema(
        name="evolve_cycles",
        version="v1",
        fields=(
            FieldSpec("cycle_id", FieldType.STRING),
            FieldSpec("outcome", FieldType.STRING),
            FieldSpec("timestamp", FieldType.TIMESTAMP),
            FieldSpec("schema_version", FieldType.STRING),
        ),
        cardinality=Cardinality.ONE_PER_EVENT,
        owner="evolution/report.py",
        description="Evolution cycle outcomes",
    ),
    "governance_log": StreamSchema(
        name="governance_log",
        version="v1",
        fields=(
            FieldSpec("event_type", FieldType.STRING),
            FieldSpec("detail", FieldType.STRING),
            FieldSpec("timestamp", FieldType.TIMESTAMP),
            FieldSpec("schema_version", FieldType.STRING),
        ),
        cardinality=Cardinality.MANY_PER_RUN,
        owner="evolution/governance.py",
        description="Governance audit log",
    ),
    "file_health": StreamSchema(
        name="file_health",
        version="v1",
        fields=(
            FieldSpec("path", FieldType.STRING),
            FieldSpec("health_score", FieldType.FLOAT),
            FieldSpec("timestamp", FieldType.TIMESTAMP),
            FieldSpec("schema_version", FieldType.STRING),
        ),
        cardinality=Cardinality.MANY_PER_RUN,
        owner="persistence/file_health.py",
        description="File health scores",
    ),
    "file_health_touches": StreamSchema(
        name="file_health_touches",
        version="v1",
        fields=(
            FieldSpec("path", FieldType.STRING),
            FieldSpec("touched_by", FieldType.STRING),
            FieldSpec("timestamp", FieldType.TIMESTAMP),
            FieldSpec("schema_version", FieldType.STRING),
        ),
        cardinality=Cardinality.MANY_PER_RUN,
        owner="persistence/file_health.py",
        description="File touch events",
    ),
    "command_policy": StreamSchema(
        name="command_policy",
        version="v1",
        fields=(
            FieldSpec("session_id", FieldType.STRING),
            FieldSpec("command", FieldType.STRING),
            FieldSpec("allowed", FieldType.BOOL),
            FieldSpec("timestamp", FieldType.TIMESTAMP),
            FieldSpec("schema_version", FieldType.STRING),
        ),
        cardinality=Cardinality.MANY_PER_TASK,
        owner="security/command_policy.py",
        description="Command policy decisions",
    ),
    "calibration": StreamSchema(
        name="calibration",
        version="v1",
        fields=(
            FieldSpec("model", FieldType.STRING),
            FieldSpec("calibration_score", FieldType.FLOAT),
            FieldSpec("timestamp", FieldType.TIMESTAMP),
            FieldSpec("schema_version", FieldType.STRING),
        ),
        cardinality=Cardinality.ONE_PER_EVENT,
        owner="eval/calibration.py",
        description="Model calibration records",
    ),
    "swe_bench_results": StreamSchema(
        name="swe_bench_results",
        version="v1",
        fields=(
            FieldSpec("instance_id", FieldType.STRING),
            FieldSpec("passed", FieldType.BOOL),
            FieldSpec("model", FieldType.STRING),
            FieldSpec("timestamp", FieldType.TIMESTAMP),
            FieldSpec("schema_version", FieldType.STRING),
        ),
        cardinality=Cardinality.ONE_PER_EVENT,
        owner="eval/swe_bench.py",
        description="SWE-bench evaluation results",
    ),
    "programbench_results": StreamSchema(
        name="programbench_results",
        version="v1",
        fields=(
            FieldSpec("instance_id", FieldType.STRING),
            FieldSpec("passed", FieldType.BOOL),
            FieldSpec("model", FieldType.STRING),
            FieldSpec("timestamp", FieldType.TIMESTAMP),
            FieldSpec("schema_version", FieldType.STRING),
        ),
        cardinality=Cardinality.ONE_PER_EVENT,
        owner="eval/programbench.py",
        description="ProgramBench evaluation results",
    ),
}


def get_schema(stream_name: str) -> StreamSchema | None:
    """Get schema for a metric stream by name."""
    return SCHEMAS.get(stream_name)


def validate_record(stream_name: str, record: dict[str, object]) -> list[str]:
    """Validate a record against its schema.

    Returns a list of validation errors. Empty list means valid.
    """
    schema = get_schema(stream_name)
    if not schema:
        return [f"Unknown stream: {stream_name}"]

    errors: list[str] = []
    record_fields = set(record.keys())
    schema_field_names = {f.name for f in schema.fields}

    # Check for unknown fields
    unknown = record_fields - schema_field_names
    if unknown:
        errors.append(f"Unknown fields: {sorted(unknown)}")

    # Check for missing required fields
    required_fields = {f.name for f in schema.fields if f.required}
    missing = required_fields - record_fields
    if missing:
        errors.append(f"Missing required fields: {sorted(missing)}")

    return errors
