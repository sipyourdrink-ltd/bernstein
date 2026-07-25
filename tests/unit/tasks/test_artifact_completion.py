"""Artifact-mode task completion end to end (#2608 slice 2).

The claims under test are the ones the feature exists for:

* an artifact-mode task **completes without a commit** - it produces a signed
  lineage receipt, and that receipt's ``entry_hash`` is the completion
  identity a coding task would have taken from a git SHA;
* the completion path is what makes the ``schema_valid`` / ``criteria_match``
  / ``hash_stable`` / ``figures_grounded`` signals reachable. Issue #2968 made
  them fail closed with no evaluator; here they are dispatched with the
  artifact in scope and genuinely pass or fail on their merits;
* **the same artifact-mode task run twice yields byte-identical output** -
  same canonical bytes, same ``content_hash``, same signed ``entry_hash``.
  This is the AC as written, over the task path, not over a library call;
* a failing criterion mints **no** receipt - a receipt asserts the declared
  gates held, so it must not exist when they did not;
* the produced receipt verifies through the shipped auditor
  (``verify_artifact``), which is what makes it evidence rather than a log line.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.lineage.artifact_record import ARTIFACT_SINK_RELPATH, load_receipt, verify_artifact
from bernstein.core.quality.janitor import verify_task
from bernstein.core.tasks.artifact_completion import (
    DEFAULT_OUTBOX_RELPATH,
    ArtifactCompletion,
    ArtifactCompletionError,
    artifact_output_path,
    complete_artifact_task,
    is_artifact_mode,
    load_artifact,
    verify_task_completion,
)
from bernstein.core.tasks.artifacts import ArtifactKind, ArtifactSpec, artifact_content_hash
from bernstein.core.tasks.models import CompletionSignal, Task

#: One fixed operator secret across a test's runs: the entry's HMAC envelope is
#: inside the hashed body, so byte-identity is a claim about one operator's two
#: runs, not about two unrelated installs.
_OPERATOR_KEY = b"o" * 64

_ROWS = [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]

_ROW_SCHEMA = json.dumps(
    {
        "type": "object",
        "required": ["id", "name"],
        "properties": {"id": {"type": "integer"}, "name": {"type": "string"}},
    }
)


def _task(
    *,
    task_id: str = "T-artifact-1",
    kind: ArtifactKind = ArtifactKind.REPORT,
    signals: list[CompletionSignal] | None = None,
    output_path: str = "",
) -> Task:
    return Task(
        id=task_id,
        title="Produce the weekly summary",
        description="A non-coding task that emits an artifact.",
        role="analyst",
        completion_signals=signals or [],
        artifact_spec=ArtifactSpec(kind=kind, output_path=output_path),
    )


def _write(workdir: Path, relpath: str, payload: str) -> None:
    path = workdir / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")


def _run(workdir: Path, task: Task) -> ArtifactCompletion:
    return complete_artifact_task(task, workdir, operator_hmac_key=_OPERATOR_KEY)


# ---------------------------------------------------------------------------
# Mode + output-path resolution
# ---------------------------------------------------------------------------


def test_code_diff_task_is_not_artifact_mode() -> None:
    assert is_artifact_mode(Task(id="T", title="t", description="d", role="backend")) is False


@pytest.mark.parametrize(
    "kind",
    [ArtifactKind.REPORT, ArtifactKind.DATASET, ArtifactKind.ACTION_LOG, ArtifactKind.OPS_RESULT],
)
def test_every_non_code_kind_is_artifact_mode(kind: ArtifactKind) -> None:
    assert is_artifact_mode(_task(kind=kind)) is True


def test_default_output_path_is_per_task_under_the_outbox() -> None:
    assert artifact_output_path(_task(task_id="T-9")) == f"{DEFAULT_OUTBOX_RELPATH}/T-9/artifact"


def test_declared_output_path_wins() -> None:
    assert artifact_output_path(_task(output_path="reports/weekly.md")) == "reports/weekly.md"


@pytest.mark.parametrize("bad", ["/etc/passwd", "../../escape.md", "reports/../../escape.md"])
def test_unsafe_output_path_is_rejected_before_any_read(bad: str) -> None:
    with pytest.raises(ArtifactCompletionError):
        artifact_output_path(_task(output_path=bad))


# ---------------------------------------------------------------------------
# Loading the produced artifact
# ---------------------------------------------------------------------------


def test_missing_output_is_a_completion_failure_not_a_crash(tmp_path: Path) -> None:
    completion = _run(tmp_path, _task())
    assert completion.ok is False
    assert completion.receipt is None
    assert any("wrote no output" in f for f in completion.failures)


def test_dataset_output_loads_as_rows(tmp_path: Path) -> None:
    task = _task(kind=ArtifactKind.DATASET, output_path="out/rows.jsonl")
    _write(tmp_path, "out/rows.jsonl", "\n".join(json.dumps(r) for r in _ROWS) + "\n")
    assert load_artifact(task, tmp_path) == _ROWS


def test_ops_result_output_loads_as_object(tmp_path: Path) -> None:
    task = _task(kind=ArtifactKind.OPS_RESULT, output_path="out/result.json")
    _write(tmp_path, "out/result.json", json.dumps({"status": "ok", "restarted": 3}))
    assert load_artifact(task, tmp_path) == {"status": "ok", "restarted": 3}


def test_non_utf8_dataset_bytes_fail_completion_cleanly(tmp_path: Path) -> None:
    task = _task(kind=ArtifactKind.DATASET, output_path="out/rows.jsonl")
    path = tmp_path / "out" / "rows.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'{"id": 1, "name": "\xff\xfe"}\n')

    completion = _run(tmp_path, task)

    assert completion.ok is False
    assert any("not valid UTF-8" in f for f in completion.failures)


def test_malformed_dataset_line_fails_completion_with_the_line_number(tmp_path: Path) -> None:
    task = _task(kind=ArtifactKind.DATASET, output_path="out/rows.jsonl")
    _write(tmp_path, "out/rows.jsonl", '{"id": 1}\nnot-json\n')
    completion = _run(tmp_path, task)
    assert completion.ok is False
    assert any("line 2" in f for f in completion.failures)


# ---------------------------------------------------------------------------
# The core acceptance criterion: completion without a commit
# ---------------------------------------------------------------------------


def test_report_task_completes_on_a_signed_receipt_with_no_commit(tmp_path: Path) -> None:
    """The end-to-end path: a report task reaches done via a lineage receipt.

    No git repository exists under ``tmp_path`` at all - which is the point.
    The completion identity is the signed entry hash, not a SHA.
    """
    body = "# Weekly summary\n\nThroughput held at 42 tasks.\n"
    task = _task(
        kind=ArtifactKind.REPORT,
        output_path="reports/weekly.md",
        signals=[CompletionSignal(type="hash_stable", value=artifact_content_hash(ArtifactKind.REPORT, body))],
    )
    _write(tmp_path, "reports/weekly.md", body)

    completion = _run(tmp_path, task)

    assert completion.ok is True, completion.failures
    assert not (tmp_path / ".git").exists()
    receipt = completion.receipt
    assert receipt is not None
    assert receipt.kind == "report"
    assert receipt.entry_hash == completion.entry_hash
    assert receipt.entry_hash.startswith("sha256:")
    assert receipt.content_hash == artifact_content_hash(ArtifactKind.REPORT, body)
    # The receipt is persisted where ``bernstein artifact verify`` looks.
    assert load_receipt(tmp_path / ARTIFACT_SINK_RELPATH, task.id) == receipt


def test_recorded_receipt_verifies_through_the_shipped_auditor(tmp_path: Path) -> None:
    body = "Ops handover.\n"
    task = _task(kind=ArtifactKind.REPORT, output_path="handover.md")
    _write(tmp_path, "handover.md", body)

    completion = _run(tmp_path, task)
    assert completion.ok is True, completion.failures

    result = verify_artifact(
        task_id=task.id,
        sink_root=tmp_path / ARTIFACT_SINK_RELPATH,
        log_path=tmp_path / ".sdd" / "lineage" / "log.jsonl",
        cards_dir=tmp_path / ".sdd" / "agents",
        operator_secret=_OPERATOR_KEY,
    )
    assert result.ok is True, result.failures
    assert result.entry_hash == completion.entry_hash


def test_tampering_with_the_stored_bytes_breaks_verification(tmp_path: Path) -> None:
    task = _task(kind=ArtifactKind.REPORT, output_path="handover.md")
    _write(tmp_path, "handover.md", "Ops handover.\n")
    assert _run(tmp_path, task).ok is True

    blob = tmp_path / ARTIFACT_SINK_RELPATH / task.id / "artifact.bin"
    blob.write_bytes(blob.read_bytes().replace(b"Ops", b"0ps"))

    result = verify_artifact(
        task_id=task.id,
        sink_root=tmp_path / ARTIFACT_SINK_RELPATH,
        log_path=tmp_path / ".sdd" / "lineage" / "log.jsonl",
        cards_dir=tmp_path / ".sdd" / "agents",
        operator_secret=_OPERATOR_KEY,
    )
    assert result.ok is False
    assert any("altered" in f for f in result.failures)


# ---------------------------------------------------------------------------
# Determinism, as the AC is written: the same TASK run twice
# ---------------------------------------------------------------------------


def test_same_artifact_task_run_twice_is_byte_identical(tmp_path: Path) -> None:
    """Two independent runs of one task produce the same signed artifact.

    Separate workdirs, separate lineage stores, separate signing key material -
    only the task, the produced bytes, and the operator secret are shared. The
    canonical bytes, the content hash, and the signed entry hash must all match;
    a divergence here is a detected non-determinism, not a flaky assertion.
    """
    task = _task(kind=ArtifactKind.DATASET, output_path="out/rows.jsonl")
    payload = "\n".join(json.dumps(r) for r in _ROWS) + "\n"

    hashes = []
    for run in ("run-a", "run-b"):
        workdir = tmp_path / run
        _write(workdir, "out/rows.jsonl", payload)
        completion = _run(workdir, task)
        assert completion.ok is True, completion.failures
        assert completion.receipt is not None
        hashes.append(
            (
                (workdir / ARTIFACT_SINK_RELPATH / task.id / "artifact.bin").read_bytes(),
                completion.receipt.content_hash,
                completion.receipt.entry_hash,
            )
        )

    assert hashes[0] == hashes[1]


def test_a_one_byte_input_change_moves_both_hashes(tmp_path: Path) -> None:
    task = _task(kind=ArtifactKind.DATASET, output_path="out/rows.jsonl")

    def run(run_name: str, rows: list[dict[str, object]]) -> tuple[str, str]:
        workdir = tmp_path / run_name
        _write(workdir, "out/rows.jsonl", "\n".join(json.dumps(r) for r in rows) + "\n")
        completion = _run(workdir, task)
        assert completion.receipt is not None
        return completion.receipt.content_hash, completion.receipt.entry_hash

    base = run("run-a", _ROWS)
    mutated = run("run-b", [{"id": 1, "name": "a"}, {"id": 2, "name": "c"}])
    assert base[0] != mutated[0]
    assert base[1] != mutated[1]


def test_row_order_is_preserved_so_reordering_is_a_real_difference(tmp_path: Path) -> None:
    """Canonicalisation normalises key order, never row order.

    A dataset is a sequence; silently sorting it would make two different
    results hash the same.
    """
    task = _task(kind=ArtifactKind.DATASET, output_path="out/rows.jsonl")

    def run(run_name: str, rows: list[dict[str, object]]) -> str:
        workdir = tmp_path / run_name
        _write(workdir, "out/rows.jsonl", "\n".join(json.dumps(r) for r in rows) + "\n")
        completion = _run(workdir, task)
        assert completion.receipt is not None
        return completion.receipt.content_hash

    assert run("a", _ROWS) != run("b", list(reversed(_ROWS)))


# ---------------------------------------------------------------------------
# The #2968 fail-closed case is now reachable *and* passing
# ---------------------------------------------------------------------------


def test_artifact_signals_fail_closed_on_the_filesystem_path(tmp_path: Path) -> None:
    """Guard on #2968: without the artifact in scope these still cannot pass."""
    task = _task(
        kind=ArtifactKind.DATASET,
        output_path="out/rows.jsonl",
        signals=[CompletionSignal(type="schema_valid", value=_ROW_SCHEMA)],
    )
    passed, failed = verify_task(task, tmp_path)
    assert passed is False
    assert any("requires artifact-mode evaluation" in f for f in failed)


def test_schema_and_criteria_signals_pass_through_the_completion_path(tmp_path: Path) -> None:
    """The same declared signals, dispatched with the artifact in scope."""
    task = _task(
        kind=ArtifactKind.DATASET,
        output_path="out/rows.jsonl",
        signals=[
            CompletionSignal(type="schema_valid", value=_ROW_SCHEMA),
            CompletionSignal(type="criteria_match", value=json.dumps([{"op": "eq", "path": "0.name", "value": "a"}])),
            CompletionSignal(type="hash_stable", value=artifact_content_hash(ArtifactKind.DATASET, _ROWS)),
        ],
    )
    _write(tmp_path, "out/rows.jsonl", "\n".join(json.dumps(r) for r in _ROWS) + "\n")

    completion = _run(tmp_path, task)

    assert completion.ok is True, completion.failures
    assert [passed for _, passed, _ in completion.signal_results] == [True, True, True]
    assert completion.receipt is not None


def test_a_failing_criterion_mints_no_receipt(tmp_path: Path) -> None:
    task = _task(
        kind=ArtifactKind.DATASET,
        output_path="out/rows.jsonl",
        signals=[CompletionSignal(type="hash_stable", value="sha256:" + "0" * 64)],
    )
    _write(tmp_path, "out/rows.jsonl", "\n".join(json.dumps(r) for r in _ROWS) + "\n")

    completion = _run(tmp_path, task)

    assert completion.ok is False
    assert completion.receipt is None
    assert any("hash drift" in f for f in completion.failures)
    assert load_receipt(tmp_path / ARTIFACT_SINK_RELPATH, task.id) is None


def test_a_failing_schema_names_the_offending_row(tmp_path: Path) -> None:
    task = _task(
        kind=ArtifactKind.DATASET,
        output_path="out/rows.jsonl",
        signals=[CompletionSignal(type="schema_valid", value=_ROW_SCHEMA)],
    )
    _write(tmp_path, "out/rows.jsonl", '{"id": 1, "name": "a"}\n{"id": "two", "name": "b"}\n')

    completion = _run(tmp_path, task)

    assert completion.ok is False
    assert any("row 1" in f for f in completion.failures)


def test_filesystem_signals_still_apply_to_an_artifact_task(tmp_path: Path) -> None:
    """An artifact task may still declare a filesystem gate; it is evaluated."""
    task = _task(
        kind=ArtifactKind.REPORT,
        output_path="notes.md",
        signals=[CompletionSignal(type="path_exists", value="evidence/source.csv")],
    )
    _write(tmp_path, "notes.md", "notes\n")

    completion = _run(tmp_path, task)
    assert completion.ok is False
    assert any("path_exists" in f for f in completion.failures)

    _write(tmp_path, "evidence/source.csv", "a,b\n")
    assert _run(tmp_path, task).ok is True


# ---------------------------------------------------------------------------
# The dispatch seam every completion path calls
# ---------------------------------------------------------------------------


def test_verify_task_completion_leaves_the_coding_path_byte_for_byte_unchanged(tmp_path: Path) -> None:
    task = Task(
        id="T-code",
        title="Fix the parser",
        description="A normal coding task.",
        role="backend",
        completion_signals=[CompletionSignal(type="path_exists", value="impl.py")],
    )
    assert verify_task_completion(task, tmp_path) == verify_task(task, tmp_path)
    _write(tmp_path, "impl.py", "x = 1\n")
    assert verify_task_completion(task, tmp_path) == verify_task(task, tmp_path) == (True, [])


def test_verify_task_completion_routes_artifact_tasks_and_records(tmp_path: Path, monkeypatch) -> None:
    key_path = tmp_path / "audit.key"
    key_path.write_text("aa" * 32, encoding="utf-8")
    key_path.chmod(0o600)
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(key_path))

    task = _task(kind=ArtifactKind.REPORT, output_path="summary.md")
    _write(tmp_path, "summary.md", "All green.\n")

    passed, failed = verify_task_completion(task, tmp_path)

    assert (passed, failed) == (True, [])
    assert load_receipt(tmp_path / ARTIFACT_SINK_RELPATH, task.id) is not None


def test_verify_task_completion_reports_a_missing_artifact_as_failure(tmp_path: Path, monkeypatch) -> None:
    key_path = tmp_path / "audit.key"
    key_path.write_text("bb" * 32, encoding="utf-8")
    key_path.chmod(0o600)
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(key_path))

    passed, failed = verify_task_completion(_task(kind=ArtifactKind.OPS_RESULT), tmp_path)

    assert passed is False
    assert failed and "wrote no output" in failed[0]


# ---------------------------------------------------------------------------
# The orchestrator seam: the pass is actually enqueued for an artifact task
# ---------------------------------------------------------------------------


class _FakeOrch:
    """Minimal stand-in for the attributes ``_enqueue_alive_exit_janitor_pass`` reads."""

    def __init__(self, workdir: Path) -> None:
        self._workdir = workdir
        self._executor = None
        self._config = None
        self._task_to_session: dict[str, str] = {}


def test_a_signal_less_artifact_task_is_still_enqueued_and_recorded(tmp_path: Path, monkeypatch) -> None:
    """The receipt *is* the completion, so the pass must run without signals.

    A signal-less coding task is auto-verified with no pass at all; an
    artifact task skipped the same way would have nothing to complete on.
    """
    from bernstein.core.tasks.task_lifecycle import _enqueue_alive_exit_janitor_pass

    key_path = tmp_path / "audit.key"
    key_path.write_text("cc" * 32, encoding="utf-8")
    key_path.chmod(0o600)
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(key_path))

    task = _task(kind=ArtifactKind.OPS_RESULT, output_path="ops.json")
    _write(tmp_path, "ops.json", json.dumps({"status": "ok"}))

    future = _enqueue_alive_exit_janitor_pass(_FakeOrch(tmp_path), task, reason="alive_exit_tick")

    assert future is not None, "artifact-mode task was skipped by the janitor enqueue"
    assert future.result() == (True, [])
    assert load_receipt(tmp_path / ARTIFACT_SINK_RELPATH, task.id) is not None


def test_a_signal_less_coding_task_still_skips_the_pass(tmp_path: Path) -> None:
    from bernstein.core.tasks.task_lifecycle import _enqueue_alive_exit_janitor_pass

    task = Task(id="T-code", title="t", description="d", role="backend")
    assert _enqueue_alive_exit_janitor_pass(_FakeOrch(tmp_path), task, reason="alive_exit_tick") is None
