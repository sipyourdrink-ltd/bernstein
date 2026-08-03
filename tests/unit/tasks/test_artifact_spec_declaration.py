"""Operator-declared artifact contracts reach the task, or stop the load (#3110).

The completion side of the artifact contract already ships and is covered by
``test_artifact_completion.py``. What this file protects is the *declaration*
surface: the same ``artifact_spec`` block, parsed by one shared strict parser,
must

* survive every operator loader (plan schema, plan loader, backlog
  frontmatter, CLI flags, the POST /tasks wire) into the resulting task;
* fail closed when malformed - refused at load with the offending field
  named, never silently downgraded to a ``code_diff`` task;
* leave an undeclared task byte-for-byte on the existing coding path; and
* actually route a declared task to the artifact completion path, with the
  same determinism guarantees the completion tests pin.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from click.testing import CliRunner
from fastapi import HTTPException

from bernstein.core.lineage.artifact_record import ARTIFACT_SINK_RELPATH
from bernstein.core.planning.plan_loader import PlanLoadError, load_plan
from bernstein.core.planning.plan_schema import validate_plan
from bernstein.core.tasks.artifact_completion import complete_artifact_task, is_artifact_mode
from bernstein.core.tasks.artifacts import (
    ArtifactKind,
    ArtifactSpec,
    ArtifactSpecError,
    parse_artifact_spec,
)
from bernstein.core.tasks.backlog_parser import BacklogParseError, parse_backlog_text
from bernstein.core.tasks.models import Task

#: One fixture block asserted across every loader, so the loaders cannot
#: drift from each other: same kind, same output path, same criterion.
_ARTIFACT_BLOCK: dict[str, Any] = {
    "kind": "report",
    "output_path": "reports/weekly.md",
    "criteria": [{"type": "criteria_match", "value": "[]"}],
}

_OPERATOR_KEY = b"o" * 64

_ROWS = [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]


def _plan_dict(artifact_block: dict[str, Any] | None) -> dict[str, Any]:
    step: dict[str, Any] = {"title": "Produce the weekly report", "role": "analyst"}
    if artifact_block is not None:
        step["artifact_spec"] = artifact_block
    return {"name": "weekly", "stages": [{"name": "report", "steps": [step]}]}


def _plan_file(tmp_path: Path, artifact_block: dict[str, Any] | None) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "plan.yaml"
    path.write_text(yaml.safe_dump(_plan_dict(artifact_block)), encoding="utf-8")
    return path


def _backlog_text(artifact_block: dict[str, Any] | None) -> str:
    front: dict[str, Any] = {"title": "Produce the weekly report", "role": "analyst"}
    if artifact_block is not None:
        front["artifact_spec"] = artifact_block
    return f"---\n{yaml.safe_dump(front)}---\n\n# Produce the weekly report\n\nBody.\n"


# ---------------------------------------------------------------------------
# AC1: one declaration, three loaders, same resulting contract
# ---------------------------------------------------------------------------


def test_plan_schema_accepts_the_artifact_block() -> None:
    assert validate_plan(_plan_dict(_ARTIFACT_BLOCK)) == []


def test_declared_artifact_reaches_task_payload_from_plan(tmp_path: Path) -> None:
    _config, tasks = load_plan(_plan_file(tmp_path, _ARTIFACT_BLOCK))
    assert len(tasks) == 1
    spec = tasks[0].artifact_spec
    assert spec.kind is ArtifactKind.REPORT
    assert spec.output_path == "reports/weekly.md"
    assert [c.to_dict() for c in spec.criteria] == [{"type": "criteria_match", "value": "[]"}]


def test_declared_artifact_reaches_task_payload_from_backlog_yaml() -> None:
    parsed = parse_backlog_text("100-weekly.md", _backlog_text(_ARTIFACT_BLOCK))
    assert parsed is not None
    payload = parsed.to_task_payload()
    assert "artifact_spec" in payload, "declaration was dropped from the POST /tasks payload"
    spec = parse_artifact_spec(payload["artifact_spec"])
    assert spec.kind is ArtifactKind.REPORT
    assert spec.output_path == "reports/weekly.md"


def test_declared_artifact_kind_survives_all_three_loaders(tmp_path: Path) -> None:
    """The same fixture block yields the same declared kind everywhere."""
    assert validate_plan(_plan_dict(_ARTIFACT_BLOCK)) == []

    _config, plan_tasks = load_plan(_plan_file(tmp_path, _ARTIFACT_BLOCK))
    plan_kind = plan_tasks[0].artifact_spec.kind

    backlog = parse_backlog_text("100-weekly.md", _backlog_text(_ARTIFACT_BLOCK))
    assert backlog is not None
    backlog_kind = ArtifactKind(str(parse_artifact_spec(backlog.to_task_payload()["artifact_spec"]).kind))

    assert plan_kind is backlog_kind is ArtifactKind.REPORT


# ---------------------------------------------------------------------------
# AC4: malformed declarations are refused, never silently code_diff
# ---------------------------------------------------------------------------


def test_malformed_artifact_block_is_refused_not_dropped(tmp_path: Path) -> None:
    bad = {"kind": "reprot", "output_path": "reports/weekly.md"}

    # Schema validation names the field.
    errors = validate_plan(_plan_dict(bad))
    assert any("artifact_spec.kind" in e for e in errors), errors

    # The plan loader refuses; no Task list is produced at all.
    with pytest.raises(PlanLoadError, match="artifact_spec.kind"):
        load_plan(_plan_file(tmp_path, bad))

    # The backlog parser refuses; the file never becomes a task.
    with pytest.raises(BacklogParseError, match="artifact_spec.kind"):
        parse_backlog_text("100-weekly.md", _backlog_text(bad))


def test_unknown_artifact_keys_fail_closed() -> None:
    with pytest.raises(ArtifactSpecError, match="artifact_spec.critera"):
        parse_artifact_spec({"kind": "report", "output_path": "r.md", "critera": []})
    with pytest.raises(ArtifactSpecError, match=r"artifact_spec.criteria\[0\].typ"):
        parse_artifact_spec(
            {"kind": "report", "output_path": "r.md", "criteria": [{"typ": "hash_stable", "value": "x"}]}
        )


@pytest.mark.parametrize(
    "block",
    [
        {"kind": "report"},  # missing output path
        {"kind": "report", "output_path": "../escape.md"},  # traversal
        {"kind": "report", "output_path": "/etc/passwd"},  # absolute
    ],
    ids=["missing", "traversal", "absolute"],
)
def test_escaping_or_missing_output_path_is_refused_at_load(block: dict[str, Any]) -> None:
    with pytest.raises(ArtifactSpecError, match="artifact_spec.output_path") as excinfo:
        parse_artifact_spec(block)
    assert excinfo.value.field == "artifact_spec.output_path"
    # And through a real loader, before any run or file read.
    with pytest.raises(BacklogParseError, match="artifact_spec.output_path"):
        parse_backlog_text("100-weekly.md", _backlog_text(block))


def test_unknown_criterion_type_is_refused_naming_the_entry() -> None:
    with pytest.raises(ArtifactSpecError, match=r"artifact_spec.criteria\[0\].type"):
        parse_artifact_spec(
            {"kind": "report", "output_path": "r.md", "criteria": [{"type": "path_exists", "value": "x"}]}
        )


def test_code_diff_declaration_takes_no_output_path() -> None:
    """An explicit code_diff restates the default; an output path on it lies."""
    assert parse_artifact_spec({"kind": "code_diff"}) == ArtifactSpec()
    with pytest.raises(ArtifactSpecError, match="artifact_spec.output_path"):
        parse_artifact_spec({"kind": "code_diff", "output_path": "out.md"})


# ---------------------------------------------------------------------------
# AC6: absent declaration leaves the coding path untouched
# ---------------------------------------------------------------------------


def test_undeclared_tasks_keep_existing_behavior(tmp_path: Path) -> None:
    _config, tasks = load_plan(_plan_file(tmp_path, None))
    assert tasks[0].artifact_spec == ArtifactSpec()
    assert is_artifact_mode(tasks[0]) is False

    parsed = parse_backlog_text("100-weekly.md", _backlog_text(None))
    assert parsed is not None
    assert parsed.artifact_spec is None
    assert "artifact_spec" not in parsed.to_task_payload()


# ---------------------------------------------------------------------------
# AC2 + AC3: a loader-produced task completes on a signed receipt,
# deterministically, with no git commit anywhere
# ---------------------------------------------------------------------------


def _dataset_plan_file(tmp_path: Path) -> Path:
    block = {"kind": "dataset", "output_path": "out/rows.jsonl"}
    return _plan_file(tmp_path, block)


def test_declared_artifact_task_completes_on_signed_receipt_without_commit(tmp_path: Path) -> None:
    _config, tasks = load_plan(_dataset_plan_file(tmp_path))
    task = tasks[0]
    assert is_artifact_mode(task) is True

    workdir = tmp_path / "run"
    out = workdir / "out" / "rows.jsonl"
    out.parent.mkdir(parents=True)
    out.write_text("\n".join(json.dumps(r) for r in _ROWS) + "\n", encoding="utf-8")

    completion = complete_artifact_task(task, workdir, operator_hmac_key=_OPERATOR_KEY)

    assert completion.ok is True, completion.failures
    assert not (workdir / ".git").exists()
    assert completion.receipt is not None
    assert completion.receipt.entry_hash.startswith("sha256:")


def test_same_declaration_run_twice_yields_identical_hashes(tmp_path: Path) -> None:
    """Two fresh workdirs (fresh ``.sdd`` each) under one operator key agree.

    Asserted by hash equality, not absence of an exception: canonical bytes,
    ``content_hash`` and ``entry_hash`` must all be byte-identical.
    """
    payload = "\n".join(json.dumps(r) for r in _ROWS) + "\n"
    observed: list[tuple[bytes, str, str]] = []
    for run in ("run-a", "run-b"):
        _config, tasks = load_plan(_dataset_plan_file(tmp_path / run))
        task = tasks[0]
        workdir = tmp_path / run / "work"
        out = workdir / "out" / "rows.jsonl"
        out.parent.mkdir(parents=True)
        out.write_text(payload, encoding="utf-8")
        completion = complete_artifact_task(task, workdir, operator_hmac_key=_OPERATOR_KEY)
        assert completion.ok is True, completion.failures
        assert completion.receipt is not None
        observed.append(
            (
                (workdir / ARTIFACT_SINK_RELPATH / task.id / "artifact.bin").read_bytes(),
                completion.receipt.content_hash,
                completion.receipt.entry_hash,
            )
        )
    assert observed[0] == observed[1]


# ---------------------------------------------------------------------------
# CLI flag path
# ---------------------------------------------------------------------------


def _invoke_add_task(args: list[str]) -> Any:
    from bernstein.cli.commands.task_cmd import add_task

    return CliRunner().invoke(add_task, args, catch_exceptions=False)


def test_cli_dry_run_payload_carries_the_declaration() -> None:
    result = _invoke_add_task(
        [
            "Weekly report",
            "--artifact-kind",
            "report",
            "--artifact-output",
            "reports/weekly.md",
            "--artifact-criterion",
            "hash_stable:sha256:" + "0" * 64,
            "--dry-run",
        ]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output[result.output.index("{") :])
    spec = parse_artifact_spec(payload["artifact_spec"])
    assert spec.kind is ArtifactKind.REPORT
    assert spec.output_path == "reports/weekly.md"
    assert spec.criteria[0].type == "hash_stable"


def test_cli_malformed_artifact_declaration_is_a_usage_error() -> None:
    result = _invoke_add_task(["t", "--artifact-kind", "report", "--artifact-output", "../escape.md", "--dry-run"])
    assert result.exit_code != 0
    assert "artifact_spec.output_path" in result.output

    result = _invoke_add_task(["t", "--artifact-output", "reports/x.md", "--dry-run"])
    assert result.exit_code != 0
    assert "--artifact-kind" in result.output


# ---------------------------------------------------------------------------
# Wire path: the declaration survives POST /tasks into the stored task
# ---------------------------------------------------------------------------


def _wire_request(payload: dict[str, Any]) -> Any:
    from bernstein.core.server.server_models import TaskCreate

    return TaskCreate(**payload)


@pytest.mark.anyio
async def test_wire_roundtrip_preserves_declaration_from_backlog_payload(tmp_path: Path) -> None:
    from bernstein.core.server.server_app import task_to_response
    from bernstein.core.tasks.task_store_core import TaskStore

    parsed = parse_backlog_text("100-weekly.md", _backlog_text(_ARTIFACT_BLOCK))
    assert parsed is not None
    req = _wire_request(dict(parsed.to_task_payload()))

    store = TaskStore(tmp_path / "runtime" / "tasks.jsonl", archive_path=tmp_path / "archive" / "tasks.jsonl")
    task = await store.create(req)
    assert task.artifact_spec.kind is ArtifactKind.REPORT
    assert task.artifact_spec.output_path == "reports/weekly.md"

    response = task_to_response(task)
    assert response.artifact_spec is not None
    rehydrated = Task.from_dict(response.model_dump())
    assert rehydrated.artifact_spec == task.artifact_spec


@pytest.mark.anyio
async def test_malformed_wire_payload_is_refused_by_the_store(tmp_path: Path) -> None:
    """Defense in depth: even a non-TaskCreate request object cannot store one."""
    from bernstein.core.tasks.task_store_core import TaskStore

    req = SimpleNamespace(
        title="T",
        description="D",
        role="backend",
        priority=2,
        scope="medium",
        complexity="medium",
        estimated_minutes=30,
        depends_on=[],
        owned_files=[],
        cell_id=None,
        task_type="standard",
        upgrade_details=None,
        model=None,
        effort=None,
        batch_eligible=False,
        completion_signals=[],
        slack_context=None,
        artifact_spec={"kind": "reprot", "output_path": "r.md"},
    )
    store = TaskStore(tmp_path / "runtime" / "tasks.jsonl", archive_path=tmp_path / "archive" / "tasks.jsonl")
    with pytest.raises(HTTPException) as excinfo:
        await store.create(req)
    assert excinfo.value.status_code == 422
    assert "artifact_spec.kind" in str(excinfo.value.detail)


def test_wire_schema_refuses_malformed_declaration_naming_the_field() -> None:
    import pydantic

    with pytest.raises(pydantic.ValidationError, match="artifact_spec.kind"):
        _wire_request({"title": "t", "description": "d", "artifact_spec": {"kind": "reprot"}})


# ---------------------------------------------------------------------------
# AC5: routing - the declared contract reaches the commit-check decision
# ---------------------------------------------------------------------------


def _artifact_task() -> Task:
    return Task(
        id="T-a",
        title="t",
        description="d",
        role="analyst",
        artifact_spec=ArtifactSpec(kind=ArtifactKind.REPORT, output_path="r.md"),
    )


def test_artifact_matrix_row_routes_past_commit_check_while_git_diff_keeps_it() -> None:
    from bernstein.adapters._contract import OutputMode, strategy_for
    from bernstein.adapters.conformance import assert_strategies_declared
    from bernstein.adapters.registry import get_adapter
    from bernstein.core.orchestration.commit_completion import (
        CompletionVerdict,
        RetryDecision,
        decide_retry,
    )

    # The axis has a consumer: the computer-use family declares artifact
    # output, and the full registry still passes the conformance harness.
    assert strategy_for("computer_use").output_mode is OutputMode.ARTIFACT
    assert_strategies_declared()

    no_commit = CompletionVerdict(committed=False, before="a" * 40, after="a" * 40, reason="head_did_not_move")

    artifact_adapter = get_adapter("computer_use")
    assert decide_retry(adapter=artifact_adapter, verdict=no_commit, exit_code=0, attempts=0) == RetryDecision(
        should_retry=False, reason="artifact_output_mode"
    )

    # A git-diff adapter keeps the commit check: the same missing commit is
    # still a retryable defect for it.
    class _GitDiffStub:
        supports_session_continuation = True

        def strategy(self) -> Any:
            from bernstein.adapters._contract import AdapterStrategy

            return AdapterStrategy()

        def continuation_args(self, session_id: str) -> list[str]:
            return ["--resume", session_id]

    git_decision = decide_retry(
        adapter=_GitDiffStub(),  # type: ignore[arg-type]
        verdict=no_commit,
        exit_code=0,
        attempts=0,
    )
    assert git_decision == RetryDecision(should_retry=True, reason="needs_retry")


def test_task_declared_contract_supplies_the_output_mode_override() -> None:
    """A declared artifact task routes past the commit check on any adapter."""
    from bernstein.adapters._contract import AdapterStrategy, OutputMode
    from bernstein.core.orchestration.commit_completion import (
        CompletionVerdict,
        RetryDecision,
        decide_retry,
        task_output_mode,
    )

    assert task_output_mode(_artifact_task()) is OutputMode.ARTIFACT
    assert task_output_mode(Task(id="T-c", title="t", description="d", role="backend")) is None

    class _GitDiffStub:
        supports_session_continuation = True

        def strategy(self) -> AdapterStrategy:
            return AdapterStrategy()

    no_commit = CompletionVerdict(committed=False, before="a" * 40, after="a" * 40, reason="head_did_not_move")
    decision = decide_retry(
        adapter=_GitDiffStub(),  # type: ignore[arg-type]
        verdict=no_commit,
        exit_code=0,
        attempts=0,
        output_mode=task_output_mode(_artifact_task()),
    )
    assert decision == RetryDecision(should_retry=False, reason="artifact_output_mode")
