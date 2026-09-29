"""Tests for observability metric schema registry."""

from __future__ import annotations

from bernstein.core.observability.schema_registry import (
    SCHEMAS,
    get_schema,
    validate_record,
)


class TestSchemaRegistry:
    def test_all_streams_registered(self) -> None:
        """All 31 metric streams are declared in the registry."""
        expected_streams = {
            "tasks",
            "evaluations",
            "test_runs",
            "cost",
            "cost_history",
            "quality_gates",
            "review_rubric",
            "routing_decisions",
            "tool_timing",
            "tool_audit",
            "anomalies",
            "guardrails",
            "rule_violations",
            "policy_violations",
            "security_incidents",
            "kill_audit",
            "cache_breaks",
            "graduation",
            "effectiveness",
            "quality_scores",
            "verification_nudges",
            "ab_test_results",
            "evolution_weights",
            "evolve_cycles",
            "governance_log",
            "file_health",
            "file_health_touches",
            "command_policy",
            "calibration",
            "swe_bench_results",
            "programbench_results",
        }
        assert set(SCHEMAS.keys()) == expected_streams

    def test_every_schema_declares_schema_version_field(self) -> None:
        """Every schema declares a schema_version field for future stamping."""
        for name, schema in SCHEMAS.items():
            field_names = {f.name for f in schema.fields}
            assert "schema_version" in field_names, f"Stream {name} missing schema_version field"

    def test_get_schema(self) -> None:
        """get_schema returns the correct schema."""
        schema = get_schema("tasks")
        assert schema is not None
        assert schema.name == "tasks"
        assert schema.version == "v1"

        unknown = get_schema("nonexistent")
        assert unknown is None

    def test_validate_record_valid(self) -> None:
        """validate_record passes for a valid record."""
        record = {
            "task_id": "abc123",
            "role": "backend",
            "status": "completed",
            "start_time": 1234567890.0,
            "schema_version": "v1",
        }
        errors = validate_record("tasks", record)
        assert errors == []

    def test_validate_record_unknown_stream(self) -> None:
        """validate_record fails for an unknown stream."""
        record = {"foo": "bar"}
        errors = validate_record("nonexistent", record)
        assert len(errors) == 1
        assert "Unknown stream" in errors[0]

    def test_validate_record_unknown_field_detected(self) -> None:
        """validate_record fails when record has an undeclared field.

        This is the guard test proving the registry can detect rogue fields.
        Writers do not yet stamp schema_version (later slice), but the
        registry can validate against it once they do.
        """
        record = {
            "task_id": "abc123",
            "role": "backend",
            "status": "completed",
            "start_time": 1234567890.0,
            "schema_version": "v1",
            "rogue_field": "should_not_exist",  # Undeclared field
        }
        errors = validate_record("tasks", record)
        assert len(errors) == 1
        assert "Unknown fields" in errors[0]
        assert "rogue_field" in errors[0]

    def test_validate_record_missing_required(self) -> None:
        """validate_record fails when a required field is missing."""
        record = {
            "task_id": "abc123",
            "role": "backend",
            # Missing required 'status' and 'start_time'
            "schema_version": "v1",
        }
        errors = validate_record("tasks", record)
        assert len(errors) == 1
        assert "Missing required fields" in errors[0]

    def test_validate_record_optional_field_ok(self) -> None:
        """validate_record allows missing optional fields."""
        record = {
            "task_id": "abc123",
            "role": "backend",
            "status": "completed",
            "start_time": 1234567890.0,
            "schema_version": "v1",
            # Optional fields like model, provider, cost_usd omitted
        }
        errors = validate_record("tasks", record)
        assert errors == []


class TestMultiStreamValidation:
    """Validate multiple metric streams against their schemas."""

    def test_evaluations_stream_schema(self) -> None:
        """Evaluations stream schema validates correctly."""
        record = {
            "task_id": "test123",
            "model": "sonnet",
            "role": "backend",
            "complexity": "simple",
            "result": "pass",
            "duration_s": 10.5,
            "cost_usd": 0.01,
            "step_count": 5,
            "quality_gate_results": {},
            "timestamp": 1234567890.0,
            "schema_version": "v1",
        }
        errors = validate_record("evaluations", record)
        assert errors == []

    def test_cost_stream_schema(self) -> None:
        """Cost stream schema validates correctly."""
        record = {
            "task_id": "test123",
            "model": "sonnet",
            "provider": "anthropic",
            "tokens_prompt": 100,
            "tokens_completion": 50,
            "cost_usd": 0.01,
            "timestamp": 1234567890.0,
            "schema_version": "v1",
        }
        errors = validate_record("cost", record)
        assert errors == []

    def test_quality_gates_stream_schema(self) -> None:
        """Quality gates stream schema validates correctly."""
        record = {
            "task_id": "test123",
            "gate": "lint",
            "passed": True,
            "blocked": False,
            "timestamp": 1234567890.0,
            "schema_version": "v1",
        }
        errors = validate_record("quality_gates", record)
        assert errors == []

    def test_guardrails_stream_schema(self) -> None:
        """Guardrails stream schema validates correctly."""
        record = {
            "event_type": "tamper_detected",
            "severity": "high",
            "detail": "Unauthorized file modification",
            "timestamp": 1234567890.0,
            "schema_version": "v1",
        }
        errors = validate_record("guardrails", record)
        assert errors == []

    def test_unknown_field_fails_across_streams(self) -> None:
        """Unknown fields are detected in any registered stream."""
        streams_to_test = [
            (
                "tasks",
                {
                    "task_id": "t1",
                    "role": "qa",
                    "status": "done",
                    "start_time": 1.0,
                    "schema_version": "v1",
                    "bogus": "x",
                },
            ),
            (
                "cost",
                {
                    "task_id": "t1",
                    "model": "m",
                    "provider": "p",
                    "tokens_prompt": 1,
                    "tokens_completion": 1,
                    "cost_usd": 0.01,
                    "timestamp": 1.0,
                    "schema_version": "v1",
                    "extra": "y",
                },
            ),
            (
                "guardrails",
                {
                    "event_type": "e",
                    "severity": "low",
                    "detail": "d",
                    "timestamp": 1.0,
                    "schema_version": "v1",
                    "invalid": "z",
                },
            ),
        ]

        for stream_name, record in streams_to_test:
            errors = validate_record(stream_name, record)
            assert len(errors) == 1, f"Stream {stream_name} should reject unknown field"
            assert "Unknown fields" in errors[0]
