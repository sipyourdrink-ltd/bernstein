"""Declared artifact contracts gate the actual signed completion receipt (#6638)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from fastapi.testclient import TestClient

from bernstein.core.lineage.artifact_record import ARTIFACT_SINK_RELPATH, load_receipt, verify_artifact
from bernstein.core.planning.plan_loader import load_plan
from bernstein.core.server import create_app
from bernstein.core.tasks.artifact_completion import complete_artifact_task
from bernstein.core.tasks.artifacts import ArtifactKind, artifact_content_hash, evaluate_criterion, parse_artifact_spec
from bernstein.core.tasks.models import CompletionSignal, Task

_OPERATOR_KEY = b"o" * 64
_SCHEMA = json.dumps({"type": "object", "required": ["approved"]})
_PREDICATE = json.dumps([{"op": "eq", "path": "approved", "value": True}])


def _declared_task(tmp_path: Path, construction: str, criteria: list[dict[str, str]]) -> Task:
    block = {"kind": "ops_result", "output_path": "result.json", "criteria": criteria}
    if construction == "plan":
        path = tmp_path / "plan.yaml"
        path.write_text(
            yaml.safe_dump({"name": "p", "stages": [{"name": "S", "steps": [{"title": "T", "artifact_spec": block}]}]}),
            encoding="utf-8",
        )
        _, tasks = load_plan(path)
        return tasks[0]
    if construction == "rest":
        app = create_app(jsonl_path=tmp_path / "runtime" / "tasks.jsonl")
        response = TestClient(app).post(
            "/tasks",
            json={"title": "T", "description": "Produce an ops result", "role": "analyst", "artifact_spec": block},
        )
        assert response.status_code == 201, response.text
        task = app.state.store.get_task(response.json()["id"])
        assert isinstance(task, Task)
        return task
    return Task(id="T", title="T", description="D", role="analyst", artifact_spec=parse_artifact_spec(block))


@pytest.mark.parametrize("construction", ["direct", "plan", "rest"])
@pytest.mark.parametrize(
    ("criterion_type", "value", "payload"),
    [
        ("schema_valid", _SCHEMA, "{}"),
        ("criteria_match", _PREDICATE, '{"approved": false}'),
        ("hash_stable", "sha256:" + "0" * 64, '{"approved": true}'),
    ],
)
def test_declared_criterion_failure_prevents_a_success_receipt(
    tmp_path: Path, construction: str, criterion_type: str, value: str, payload: str
) -> None:
    task = _declared_task(tmp_path, construction, [{"type": criterion_type, "value": value}])
    assert task.completion_signals == []
    (tmp_path / "result.json").write_text(payload, encoding="utf-8")

    completion = complete_artifact_task(task, tmp_path, operator_hmac_key=_OPERATOR_KEY)

    assert completion.ok is False
    assert completion.receipt is None
    assert len(completion.signal_results) == 1
    assert completion.signal_results[0][0] == f"{criterion_type}: {value}"
    assert completion.signal_results[0][1] is False
    assert any(criterion_type in failure for failure in completion.failures)
    assert load_receipt(tmp_path / ARTIFACT_SINK_RELPATH, task.id) is None
    assert not (tmp_path / ".sdd" / "lineage" / "log.jsonl").exists()


@pytest.mark.parametrize("construction", ["direct", "plan", "rest"])
def test_declared_criteria_verify_the_canonical_bytes_in_the_receipt(tmp_path: Path, construction: str) -> None:
    criteria = [
        {"type": "schema_valid", "value": _SCHEMA},
        {"type": "criteria_match", "value": _PREDICATE},
        {"type": "hash_stable", "value": artifact_content_hash(ArtifactKind.OPS_RESULT, {"approved": True})},
    ]
    task = _declared_task(tmp_path, construction, criteria)
    (tmp_path / "result.json").write_text('{ "approved" : true }\n', encoding="utf-8")

    completion = complete_artifact_task(task, tmp_path, operator_hmac_key=_OPERATOR_KEY)

    assert completion.ok is True, completion.failures
    assert [passed for _, passed, _ in completion.signal_results] == [True, True, True]
    assert (tmp_path / ARTIFACT_SINK_RELPATH / task.id / "artifact.bin").read_bytes() == b'{"approved":true}'
    verification = verify_artifact(
        task_id=task.id,
        sink_root=tmp_path / ARTIFACT_SINK_RELPATH,
        log_path=tmp_path / ".sdd" / "lineage" / "log.jsonl",
        cards_dir=tmp_path / ".sdd" / "agents",
        operator_secret=_OPERATOR_KEY,
    )
    assert verification.ok is True, verification.failures
    assert verification.entry_hash == completion.entry_hash


def test_repeated_artifact_gates_evaluate_once_in_declaration_order(tmp_path: Path) -> None:
    criteria = [{"type": "schema_valid", "value": _SCHEMA}] * 2
    task = _declared_task(tmp_path, "direct", criteria)
    task.completion_signals = [
        CompletionSignal(type="schema_valid", value=_SCHEMA),
        CompletionSignal(type="criteria_match", value=_PREDICATE),
        CompletionSignal(type="criteria_match", value=_PREDICATE),
    ]
    (tmp_path / "result.json").write_text('{"approved": true}', encoding="utf-8")

    with patch("bernstein.core.tasks.artifacts.evaluate_criterion", wraps=evaluate_criterion) as evaluator:
        completion = complete_artifact_task(task, tmp_path, operator_hmac_key=_OPERATOR_KEY)

    assert completion.ok is True, completion.failures
    assert [(call.args[0], call.args[1]) for call in evaluator.call_args_list] == [
        ("schema_valid", _SCHEMA),
        ("criteria_match", _PREDICATE),
    ]
    assert len(completion.signal_results) == 2
    assert len(task.artifact_spec.criteria) == 2
    assert len(task.completion_signals) == 3


def test_declared_contract_and_filesystem_gates_both_control_receipting(tmp_path: Path) -> None:
    task = _declared_task(tmp_path, "direct", [{"type": "schema_valid", "value": _SCHEMA}])
    task.completion_signals = [CompletionSignal(type="path_exists", value="evidence.csv")]
    (tmp_path / "result.json").write_text('{"approved": true}', encoding="utf-8")

    blocked = complete_artifact_task(task, tmp_path, operator_hmac_key=_OPERATOR_KEY)
    assert blocked.ok is False
    assert blocked.receipt is None
    assert [passed for _, passed, _ in blocked.signal_results] == [True, False]
    assert load_receipt(tmp_path / ARTIFACT_SINK_RELPATH, task.id) is None

    (tmp_path / "evidence.csv").write_text("proof\n", encoding="utf-8")
    passed = complete_artifact_task(task, tmp_path, operator_hmac_key=_OPERATOR_KEY)
    assert passed.ok is True, passed.failures
    assert [passed for _, passed, _ in passed.signal_results] == [True, True]


def test_missing_declared_artifact_fails_closed_before_receipting(tmp_path: Path) -> None:
    task = _declared_task(tmp_path, "direct", [{"type": "schema_valid", "value": _SCHEMA}])
    completion = complete_artifact_task(task, tmp_path, operator_hmac_key=_OPERATOR_KEY)
    assert completion.ok is False
    assert completion.receipt is None
    assert any("wrote no output" in failure for failure in completion.failures)
    assert load_receipt(tmp_path / ARTIFACT_SINK_RELPATH, task.id) is None
