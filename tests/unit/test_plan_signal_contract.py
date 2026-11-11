"""Declared plan gates survive validation, the task wire, and completion (#6646)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from click.testing import CliRunner
from jsonschema import Draft202012Validator

from bernstein.cli.commands.dry_run_cmd import dry_run_cmd
from bernstein.cli.commands.plan_validate_cmd import validate_plan as validate_command
from bernstein.core.planning.plan_loader import PlanLoadError, load_plan
from bernstein.core.planning.plan_schema import get_plan_schema, validate_plan
from bernstein.core.planning.planner import _post_task_to_server
from bernstein.core.server import create_app
from bernstein.core.tasks.artifact_completion import complete_artifact_task, is_artifact_mode
from bernstein.core.tasks.models import Task

# Examples are independent of the registry under test: omitting one supported
# native type from a shared constant must not make its test disappear too.
SIGNALS = {
    "path_exists": "report.txt",
    "glob_exists": "reports/*.txt",
    "test_passes": "pytest -q",
    "file_contains": "report.txt :: ready",
    "llm_review": "Review the result",
    "llm_judge": "Judge the result",
    "schema_valid": '{"type":"object","required":["ok"]}',
    "criteria_match": '[{"path":"ok","op":"eq","value":true}]',
    "hash_stable": "a" * 64,
    "figures_grounded": "strict",
    "absence_verified": "tool-call-123",
}


def _plan(signals: object, **fields: object) -> dict[str, Any]:
    step: dict[str, Any] = {"title": "Write report", "role": "docs", "completion_signals": signals}
    step.update(fields)
    return {"name": "signal-contract", "stages": [{"name": "report", "steps": [step]}]}


def _write(tmp_path: Path, document: dict[str, Any]) -> Path:
    path = tmp_path / "plan.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


@pytest.mark.parametrize("kind,value", SIGNALS.items())
def test_supported_gate_survives_every_plan_validation_boundary(tmp_path: Path, kind: str, value: str) -> None:
    document = _plan([{"type": kind, "value": value}])
    assert validate_plan(document) == []
    assert list(Draft202012Validator(get_plan_schema()).iter_errors(document)) == []
    _, tasks = load_plan(_write(tmp_path, document))
    assert [(signal.type, signal.value) for signal in tasks[0].completion_signals] == [(kind, value)]


@pytest.mark.parametrize("signals", [None, {}, "", False, 0, "schema_valid"])
def test_declared_nonarray_gate_list_is_never_silently_discarded(tmp_path: Path, signals: object) -> None:
    with pytest.raises(PlanLoadError, match="report.*completion_signals"):
        load_plan(_write(tmp_path, _plan(signals)))


@pytest.mark.parametrize(
    "signal",
    [
        "schema_valid",
        {},
        {"type": "unknown", "value": "x"},
        {"type": ["schema_valid"], "value": "x"},
        {"type": {"name": "schema_valid"}, "value": "x"},
        {"type": "path_exists", "value": ["report.json"]},
        {"type": "path_exists", "value": {"path": "report.json"}},
        {"type": "path_exists", "value": 7},
        {"type": "path_exists", "value": "   "},
        {"type": "path_exists"},
    ],
)
def test_malformed_declared_gate_fails_closed_at_all_plan_boundaries(tmp_path: Path, signal: object) -> None:
    document = _plan([signal])
    assert validate_plan(document)
    assert list(Draft202012Validator(get_plan_schema()).iter_errors(document))
    with pytest.raises(PlanLoadError, match=r"report.*completion_signals\[0\]"):
        load_plan(_write(tmp_path, document))


def test_explicit_step_adapter_is_recognized_without_hiding_unrelated_typos(tmp_path: Path) -> None:
    document = _plan([], cli="codex", model="example-model", effort="high")
    warnings: list[str] = []
    assert validate_plan(document, warnings=warnings) == []
    assert warnings == []
    assert list(Draft202012Validator(get_plan_schema()).iter_errors(document)) == []
    path = _write(tmp_path, document)
    _, tasks = load_plan(path)
    assert (tasks[0].cli, tasks[0].model, tasks[0].effort) == ("codex", "example-model", "high")
    result = CliRunner().invoke(validate_command, [str(path)])
    assert result.exit_code == 0, result.output
    assert "unknown key" not in result.output.lower()
    document["stages"][0]["steps"][0]["clli"] = "codex"
    warnings = []
    validate_plan(document, warnings=warnings)
    assert any("clli" in warning for warning in warnings)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,value", SIGNALS.items())
async def test_existing_native_gate_survives_real_task_wire_and_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, value: str
) -> None:
    fields: dict[str, object] = {"cli": "codex", "model": "example-model", "effort": "high"}
    if kind in {"schema_valid", "criteria_match", "hash_stable", "figures_grounded"}:
        fields["artifact_spec"] = {"kind": "ops_result", "output_path": "report.json"}
    _, loaded = load_plan(_write(tmp_path, _plan([{"type": kind, "value": value}], **fields)))
    task = loaded[0]
    # This issue concerns the native signal contract, with the same no-auth
    # setting as the suite's autouse API fixture. Exercise the actual route.
    monkeypatch.setenv("BERNSTEIN_AUTH_DISABLED", "1")
    app = create_app(jsonl_path=tmp_path / ".sdd/runtime/tasks.jsonl")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
        identifier = await _post_task_to_server(client, "http://local-fixture", task)
    stored = app.state.store.get_task(identifier)
    assert isinstance(stored, Task)
    assert stored.artifact_spec == task.artifact_spec
    replayed = Task.from_dict(stored.to_dict())
    assert [(signal.type, signal.value) for signal in replayed.completion_signals] == [(kind, value)]
    assert (replayed.cli, replayed.model, replayed.effort) == ("codex", "example-model", "high")


def test_loaded_schema_gate_blocks_invalid_artifact_before_minting_completion(tmp_path: Path) -> None:
    document = _plan(
        [{"type": "schema_valid", "value": SIGNALS["schema_valid"]}],
        artifact_spec={"kind": "ops_result", "output_path": "report.json"},
    )
    _, tasks = load_plan(_write(tmp_path, document))
    task = tasks[0]
    (tmp_path / "report.json").write_text("{}", encoding="utf-8")
    invalid = complete_artifact_task(task, tmp_path, operator_hmac_key=b"local-plan-test-key" * 4)
    assert not invalid.ok
    assert invalid.receipt is None
    assert any("ok" in failure for failure in invalid.failures)
    (tmp_path / "report.json").write_text(json.dumps({"ok": True}), encoding="utf-8")
    valid = complete_artifact_task(task, tmp_path, operator_hmac_key=b"local-plan-test-key" * 4)
    assert valid.ok, valid.failures
    assert valid.receipt is not None


def test_undeclared_plan_gates_keep_the_existing_code_diff_path(tmp_path: Path) -> None:
    document = _plan([])
    del document["stages"][0]["steps"][0]["completion_signals"]
    _, tasks = load_plan(_write(tmp_path, document))
    assert tasks[0].completion_signals == []
    assert not is_artifact_mode(tasks[0])


@pytest.mark.parametrize(
    "signal,expected",
    [
        ({"type": "path_exists", "path": " report.txt "}, " report.txt "),
        ({"type": "glob_exists", "path": "reports/*.txt"}, "reports/*.txt"),
        ({"type": "test_passes", "command": "pytest -q"}, "pytest -q"),
        ({"type": "file_contains", "path": "report.txt", "contains": "ready"}, "report.txt :: ready"),
        ({"type": "file_contains", "value": "report.txt :: ready"}, "report.txt :: ready"),
    ],
)
def test_existing_gate_aliases_preserve_exact_values(tmp_path: Path, signal: dict[str, str], expected: str) -> None:
    document = _plan([signal])
    assert validate_plan(document) == []
    assert list(Draft202012Validator(get_plan_schema()).iter_errors(document)) == []
    _, tasks = load_plan(_write(tmp_path, document))
    assert tasks[0].completion_signals[0].value == expected


@pytest.mark.parametrize(
    "signal",
    [
        {"type": "file_contains", "value": "report.txt"},
        {"type": "file_contains", "path": "report.txt"},
        {"type": "file_contains", "path": ["report.txt"], "contains": "ready"},
        {"type": "file_contains", "path": "report.txt", "contains": False},
        {"type": "path_exists", "path": "  "},
        {"type": "test_passes", "command": []},
        {"type": "path_exists", "value": "report.txt", "path": 3},
    ],
)
def test_malformed_gate_aliases_fail_before_dispatch(tmp_path: Path, signal: dict[str, object]) -> None:
    document = _plan([signal])
    assert validate_plan(document)
    assert list(Draft202012Validator(get_plan_schema()).iter_errors(document))
    with pytest.raises(PlanLoadError, match=r"report.*completion_signals\[0\]"):
        load_plan(_write(tmp_path, document))


_EXAMPLE_PLANS = sorted((Path(__file__).resolve().parents[2] / "examples" / "plans").glob("*.yaml"))


@pytest.mark.parametrize("plan", _EXAMPLE_PLANS, ids=lambda path: path.name)
def test_bundled_plan_validates_and_previews_without_agent_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, plan: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    validation = CliRunner().invoke(validate_command, [str(plan)])
    assert validation.exit_code == 0, validation.output
    _, tasks = load_plan(plan)
    preview = CliRunner().invoke(dry_run_cmd, ["--plan", str(plan), "--json"])
    assert preview.exit_code == 0, preview.output
    rendered = json.loads(preview.stdout)["tasks"]
    assert [item["id"] for item in rendered] == [task.id for task in tasks]


def test_published_completion_signal_types_match_the_native_contract() -> None:
    snapshot = Path(__file__).resolve().parents[2] / "docs/reference/openapi.json"
    published = json.loads(snapshot.read_text(encoding="utf-8"))
    live = create_app().openapi()
    for specification in (published, live):
        signal_type = specification["components"]["schemas"]["CompletionSignalSchema"]["properties"]["type"]
        assert signal_type["enum"] == list(SIGNALS)
