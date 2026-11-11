"""Run-helper classification and capture (#5322, capture and classification only)."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.runs_cmd import runs_group
from bernstein.core.persistence.cas_store import CASStore
from bernstein.core.replay.journal import EventJournal, run_journal_path
from bernstein.core.worktrees.run_helpers import (
    RunHelper,
    capture_helpers_for_run,
    classify_run_helpers,
    read_run_helper_records,
    run_helpers_path,
)

if TYPE_CHECKING:
    from pathlib import Path

RUN_ID = "run-helper-1"


def _journal(repo: Path, *rows: tuple[str, dict[str, object]]) -> EventJournal:
    journal = EventJournal(run_id=RUN_ID, sdd_dir=repo / ".sdd")
    for event, data in rows:
        journal.record(event, **data)
    return journal


def _worktree(repo: Path, files: dict[str, bytes]) -> Path:
    worktree = repo / "wt"
    for rel, body in files.items():
        target = worktree / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    worktree.mkdir(exist_ok=True)
    return worktree


def test_unexecuted_scaffolding_is_not_a_helper(tmp_path: Path) -> None:
    worktree = _worktree(tmp_path, {"scratch/setup.py": b"print('x')\n"})
    _journal(tmp_path, ("file_create", {"path": "scratch/setup.py"}))

    assert capture_helpers_for_run(tmp_path, worktree, RUN_ID) == []
    assert read_run_helper_records(tmp_path / ".sdd", RUN_ID) == []
    assert not (tmp_path / ".sdd" / "cas").exists()


def test_executed_helper_gets_origin_step_and_hash(tmp_path: Path) -> None:
    body = b"raise SystemExit(1)\n"
    worktree = _worktree(tmp_path, {"repro.py": body})
    journal = _journal(
        tmp_path,
        ("task_claimed", {"task_id": "T-1"}),
        ("file_create", {"path": "repro.py"}),
        ("file_execute", {"path": "repro.py", "exit_code": 1}),
        ("file_execute", {"path": "repro.py", "exit_code": 1}),
    )

    [record] = capture_helpers_for_run(tmp_path, worktree, RUN_ID)

    assert record.path == "repro.py"
    assert record.origin_step == 1
    assert record.execution_count == 2
    assert record.exit_codes == (1, 1)
    assert record.content_hash == hashlib.sha256(body).hexdigest()
    assert record.run_id == RUN_ID
    assert record.journal_head == journal.head()
    cas = CASStore(tmp_path / ".sdd" / "cas")
    assert cas.get(record.content_hash) == body
    assert cas.has(record.record_hash)
    assert read_run_helper_records(tmp_path / ".sdd", RUN_ID) == [record]


def test_nonzero_exit_still_counts_as_helper() -> None:
    events = [
        {"index": 1, "event": "file_create", "path": "harness.py"},
        {"index": 2, "event": "file_execute", "path": "harness.py", "exit_code": 2},
    ]
    assert classify_run_helpers(events) == [
        RunHelper(path="harness.py", origin_step=1, execution_count=1, exit_codes=(2,))
    ]


def test_execute_without_create_is_not_a_helper() -> None:
    events = [{"index": 0, "event": "file_execute", "path": "vendor/bin", "exit_code": 0}]
    assert classify_run_helpers(events) == []


def test_missing_or_null_exit_code_is_unknown_not_zero() -> None:
    events = [
        {"index": 0, "event": "file_create", "path": "probe.sh"},
        {"index": 1, "event": "file_execute", "path": "probe.sh"},
        {"index": 2, "event": "file_execute", "path": "probe.sh", "exit_code": None},
        {"index": 3, "event": "file_execute", "path": "probe.sh", "exit_code": "boom"},
        {"index": 4, "event": "file_execute", "path": "probe.sh", "exit_code": True},
        {"index": 5, "event": "file_execute", "path": "probe.sh", "exit_code": 0},
    ]
    [helper] = classify_run_helpers(events)
    assert helper.exit_codes == (None, None, None, None, 0)


@pytest.mark.parametrize(
    "raw",
    ["C:/repo/helper.py", "c:helper.py", "/etc/passwd", "~/x.sh", "../x.py", "a/../b.py", "//srv/share/x"],
)
def test_path_outside_the_worktree_is_not_classified(raw: str) -> None:
    events = [
        {"index": 0, "event": "file_create", "path": raw},
        {"index": 1, "event": "file_execute", "path": raw, "exit_code": 0},
    ]
    assert classify_run_helpers(events) == []


def test_symlink_escaping_the_worktree_is_not_captured(tmp_path: Path) -> None:
    outside = tmp_path / "outside.py"
    outside.write_bytes(b"secret\n")
    worktree = _worktree(tmp_path, {})
    try:
        (worktree / "link.py").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable on this host")
    _journal(
        tmp_path,
        ("file_create", {"path": "link.py"}),
        ("file_execute", {"path": "link.py", "exit_code": 0}),
    )

    assert capture_helpers_for_run(tmp_path, worktree, RUN_ID) == []
    cas = CASStore(tmp_path / ".sdd" / "cas")
    assert not cas.has(hashlib.sha256(b"secret\n").hexdigest())


def test_identical_bytes_keep_distinct_records(tmp_path: Path) -> None:
    body = b"echo same\n"
    worktree = _worktree(tmp_path, {"a.sh": body, "b.sh": body})
    _journal(
        tmp_path,
        ("file_create", {"path": "a.sh"}),
        ("file_execute", {"path": "a.sh", "exit_code": 0}),
        ("file_create", {"path": "b.sh"}),
        ("file_execute", {"path": "b.sh", "exit_code": 3}),
    )

    first, second = capture_helpers_for_run(tmp_path, worktree, RUN_ID)

    assert first.content_hash == second.content_hash
    assert first.record_hash != second.record_hash
    assert (first.path, first.exit_codes) == ("a.sh", (0,))
    assert (second.path, second.exit_codes) == ("b.sh", (3,))
    assert read_run_helper_records(tmp_path / ".sdd", RUN_ID) == [first, second]


def test_capture_leaves_the_run_journal_untouched(tmp_path: Path) -> None:
    worktree = _worktree(tmp_path, {"repro.py": b"pass\n"})
    _journal(
        tmp_path,
        ("file_create", {"path": "repro.py"}),
        ("file_execute", {"path": "repro.py", "exit_code": 0}),
    )
    journal_file = run_journal_path(tmp_path / ".sdd", RUN_ID)
    before = journal_file.read_bytes()

    assert len(capture_helpers_for_run(tmp_path, worktree, RUN_ID)) == 1

    assert journal_file.read_bytes() == before
    assert run_helpers_path(tmp_path / ".sdd", RUN_ID).parent == journal_file.parent


def test_store_failure_skips_one_helper_and_keeps_the_rest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    worktree = _worktree(tmp_path, {"bad.py": b"bad\n", "good.py": b"good\n"})
    _journal(
        tmp_path,
        ("file_create", {"path": "bad.py"}),
        ("file_execute", {"path": "bad.py", "exit_code": 0}),
        ("file_create", {"path": "good.py"}),
        ("file_execute", {"path": "good.py", "exit_code": 0}),
    )
    real_put = CASStore.put

    def put(self: CASStore, content: bytes, *args: object, **kwargs: object) -> str:
        if content == b"bad\n":
            raise RuntimeError("store refused the blob")
        return real_put(self, content, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(CASStore, "put", put)

    [record] = capture_helpers_for_run(tmp_path, worktree, RUN_ID)
    assert record.path == "good.py"


def test_torn_journal_tail_does_not_raise(tmp_path: Path) -> None:
    worktree = _worktree(tmp_path, {"repro.py": b"pass\n"})
    _journal(
        tmp_path,
        ("file_create", {"path": "repro.py"}),
        ("file_execute", {"path": "repro.py", "exit_code": 0}),
    )
    with run_journal_path(tmp_path / ".sdd", RUN_ID).open("a", encoding="utf-8") as fh:
        fh.write('{"index": 2, "event": "file_exe')

    [record] = capture_helpers_for_run(tmp_path, worktree, RUN_ID)
    assert record.path == "repro.py"


def test_unsafe_run_id_captures_nothing(tmp_path: Path) -> None:
    worktree = _worktree(tmp_path, {"repro.py": b"pass\n"})
    assert capture_helpers_for_run(tmp_path, worktree, "../escape") == []


def test_edited_sidecar_row_is_not_read_back(tmp_path: Path) -> None:
    worktree = _worktree(tmp_path, {"repro.py": b"pass\n"})
    _journal(
        tmp_path,
        ("file_create", {"path": "repro.py"}),
        ("file_execute", {"path": "repro.py", "exit_code": 1}),
    )
    capture_helpers_for_run(tmp_path, worktree, RUN_ID)
    sidecar = run_helpers_path(tmp_path / ".sdd", RUN_ID)
    row = json.loads(sidecar.read_text(encoding="utf-8"))
    row["record"]["exit_codes"] = [0]
    sidecar.write_text(json.dumps(row) + "\n", encoding="utf-8")

    assert read_run_helper_records(tmp_path / ".sdd", RUN_ID) == []


def test_runs_helpers_lists_captured_records(tmp_path: Path) -> None:
    worktree = _worktree(tmp_path, {"repro.py": b"pass\n"})
    _journal(
        tmp_path,
        ("file_create", {"path": "repro.py"}),
        ("file_execute", {"path": "repro.py"}),
    )
    [record] = capture_helpers_for_run(tmp_path, worktree, RUN_ID)

    result = CliRunner().invoke(runs_group, ["helpers", RUN_ID, "--workdir", str(tmp_path), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["run_id"] == RUN_ID
    assert payload["helpers"] == [{**record.to_dict(), "record_hash": record.record_hash}]
    assert payload["helpers"][0]["exit_codes"] == [None]


def test_runs_helpers_with_nothing_recorded(tmp_path: Path) -> None:
    result = CliRunner().invoke(runs_group, ["helpers", RUN_ID, "--workdir", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "No run helpers recorded" in result.output


def test_runs_helpers_refuses_an_unsafe_run_id(tmp_path: Path) -> None:
    result = CliRunner().invoke(runs_group, ["helpers", "../escape", "--workdir", str(tmp_path)])

    assert result.exit_code == 2
