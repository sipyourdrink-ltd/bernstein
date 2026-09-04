import json

import pytest
from jsonschema import ValidationError, validate


def scorecard_schema():
    """Load Scorecard JSON Schema."""
    schema_path = "src/bernstein/core/replay/scorecard_schema.json"
    with open(schema_path, encoding="utf-8") as schema_file:
        schema = json.load(schema_file)
    return schema


def test_valid_scorecard():
    """Test a valid scorecard passes validation."""
    scorecard = {
        "schema_version": "1.0.0",
        "type_version": 1,
        "type": "https://bernstein.run/attestations/scorecard/v1",
        "run_id": "run-123",
        "trajectory": {
            "steps": 10,
            "shape": "linear",
            "first_step_seq": {"event_index": 1},
            "last_step_seq": {"event_index": 10},
        },
        "verification": {
            "journal_ok": True,
            "journal_head": "abcd1234",
            "divergent_step": None,
            "spine_ok": True,
            "spine_head": "abcd1234",
            "spine_entries": 5,
        },
        "recovery": {"repaired": False, "dropped_rows": 0, "first_recoverable_seq": None},
        "state_consistency": {"mutation_count": 15, "disagreement_count": 0, "last_mutation_event_index": None},
        "safety": {"capability_declared": True, "refusal_count": 0, "run_receipt_signed": True},
        "replayability": {"recorded": True, "key_scheme": "v1", "gateway_mode": "secure", "fixture_present": True},
    }

    schema = scorecard_schema()
    validate(instance=scorecard, schema=schema)


def test_invalid_scorecard_missing_fields():
    """Test missing required fields triggers validation error."""
    invalid_scorecard = {
        "schema_version": "1.0.0",
        "type_version": 1,
        "type": "https://bernstein.run/attestations/scorecard/v1",
        "run_id": "run-123",
    }  # Missing sections trajectory, verification... etc

    schema = scorecard_schema()
    with pytest.raises(ValidationError):
        validate(instance=invalid_scorecard, schema=schema)
