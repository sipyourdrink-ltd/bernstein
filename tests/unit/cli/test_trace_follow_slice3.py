"""`bernstein trace follow <entity-id>`: --since, --out, and live mode (#5114 slice 3).

Slice 1 (`tests/unit/cli/test_trace_follow.py`) covers `follow` over the trace store alone.
Slice 2 (`tests/unit/cli/test_trace_follow_ledger_join.py`) joins trace store and work ledger.
Slice 3 adds:
- `--since <entry-id>` to resume after a specific journal entry.
- `--out <path>` to write the per-entity trace output to a file.
- `--live` mode to stream entries until the run reaches a terminal state.
"""

from __future__ import annotations

import json
import threading
import time
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.advanced_cmd import trace_cmd
from bernstein.core.observability.trace_store import (
    ContentAddressedTraceStore,
    TraceMetadataHints,
)
from bernstein.core.persistence.work_ledger import WorkLedger

if TYPE_CHECKING:
    from pathlib import Path


def _follow(traces_dir: Path, *args: str) -> tuple[int, str]:
    result = CliRunner().invoke(trace_cmd, ["--traces-dir", str(traces_dir), "follow", *args])
    return result.exit_code, result.output


@pytest.fixture
def sdd_dir(tmp_path: Path) -> Path:
    return tmp_path / ".sdd"


@pytest.fixture
def traces_dir(sdd_dir: Path) -> Path:
    traces = sdd_dir / "traces"
    traces.mkdir(parents=True)
    return traces


def _ledger_dir(sdd_dir: Path, run_id: str) -> Path:
    path = sdd_dir / "runtime" / "ledger" / run_id
    path.mkdir(parents=True)
    return path


def test_since_resumes_across_merged_sources(sdd_dir: Path, traces_dir: Path) -> None:
    """`--since` works seamlessly across joined trace and ledger entries."""
    store = ContentAddressedTraceStore(traces_dir)
    store.put(b'{"event": "trace-1"}', hints=TraceMetadataHints(trace_id="trace-1", task_id="T-10", started_at=100.0))
    store.put(b'{"event": "trace-2"}', hints=TraceMetadataHints(trace_id="trace-2", task_id="T-10", started_at=300.0))

    run_dir = _ledger_dir(sdd_dir, "run-10")
    ledger = WorkLedger.open(run_dir)
    ledger.append(kind="agent.claimed", task_id="T-10", payload={"note": "claimed"})
    ledger.close()

    # Rewrite ledger entry timestamp to 200.0 (between trace-1 and trace-2)
    bucket = run_dir / "000000.jsonl"
    row = json.loads(bucket.read_text(encoding="utf-8").strip())
    row["ts"] = 200.0
    bucket.write_text(json.dumps(row) + "\n", encoding="utf-8")

    # Resume after trace-1 -> should return ledger entry and trace-2
    code, output = _follow(traces_dir, "T-10", "--since", "trace-1", "--as-json")
    assert code == 0
    rows = json.loads(output)
    assert len(rows) == 2
    assert rows[0]["source"] == "ledger"
    assert rows[1]["source"] == "trace"
    assert rows[1]["trace_id"] == "trace-2"

    # Resume after the ledger entry -> should return only trace-2
    code, output = _follow(traces_dir, "T-10", "--since", "ledger:run-10:0", "--as-json")
    assert code == 0
    rows = json.loads(output)
    assert len(rows) == 1
    assert rows[0]["trace_id"] == "trace-2"

    # Resume after run-10:0 spelling -> should also match
    code, output = _follow(traces_dir, "T-10", "--since", "run-10:0", "--as-json")
    assert code == 0
    rows = json.loads(output)
    assert len(rows) == 1
    assert rows[0]["trace_id"] == "trace-2"


def test_out_writes_merged_trace_and_ledger_entries(sdd_dir: Path, traces_dir: Path, tmp_path: Path) -> None:
    """`--out` writes joined entries to file."""
    store = ContentAddressedTraceStore(traces_dir)
    store.put(b'{"event": "trace-a"}', hints=TraceMetadataHints(trace_id="trace-a", task_id="T-11", started_at=100.0))

    ledger = WorkLedger.open(_ledger_dir(sdd_dir, "run-11"))
    ledger.append(kind="agent.claimed", task_id="T-11", payload={})
    ledger.close()

    # Write JSON output
    out_json = tmp_path / "reports" / "per_entity.json"
    code, _ = _follow(traces_dir, "T-11", "--out", str(out_json), "--as-json")
    assert code == 0
    assert out_json.exists()
    rows = json.loads(out_json.read_text(encoding="utf-8"))
    assert len(rows) == 2
    assert {r["source"] for r in rows} == {"trace", "ledger"}

    # Write human table output
    out_txt = tmp_path / "reports" / "per_entity.txt"
    code, _ = _follow(traces_dir, "T-11", "--out", str(out_txt))
    assert code == 0
    assert out_txt.exists()
    content = out_txt.read_text(encoding="utf-8")
    assert "trace-a" in content
    assert "run-11" in content


def test_live_follow_already_closed_run_terminates_immediately(sdd_dir: Path, traces_dir: Path) -> None:
    """A run with `run.closed` in the ledger terminates without polling indefinitely."""
    ledger = WorkLedger.open(_ledger_dir(sdd_dir, "run-closed"))
    ledger.append(kind="agent.claimed", task_id="T-done", payload={})
    ledger.append(kind="run.closed", task_id="", payload={"status": "success"})
    ledger.close()

    code, output = _follow(traces_dir, "T-done", "--live", "--poll-interval", "0.01")
    assert code == 0
    assert "Ledger entries referencing T-done" in output


def test_live_follow_streams_new_entries_until_run_terminates(sdd_dir: Path, traces_dir: Path) -> None:
    """Live follow polls for new entries and stops when `run.closed` is recorded."""
    run_path = _ledger_dir(sdd_dir, "run-active")
    ledger = WorkLedger.open(run_path)
    ledger.append(kind="agent.claimed", task_id="T-live", payload={})
    ledger.close()

    def _append_later() -> None:
        time.sleep(0.05)
        bg_ledger = WorkLedger.open(run_path)
        bg_ledger.append(kind="task.completed", task_id="T-live", payload={"outcome": "ok"})
        bg_ledger.append(kind="run.closed", task_id="", payload={"status": "done"})
        bg_ledger.close()

    writer_thread = threading.Thread(target=_append_later)
    writer_thread.start()

    code, output = _follow(traces_dir, "T-live", "--live", "--poll-interval", "0.01")
    writer_thread.join()

    assert code == 0
    assert "task.completed" in output
    assert "run terminated" in output


def test_since_with_live_does_not_replay_history_before_the_resume_point(sdd_dir: Path, traces_dir: Path) -> None:
    """`--since` + `--live` is "reconnect without replaying", so it must not replay.

    The poll loop re-collects unfiltered, so seeding the seen set from the
    post-`--since` rows made every pre-since entry look new on the first poll --
    the command replayed exactly the history it was asked to skip.
    """
    run_path = _ledger_dir(sdd_dir, "run-resume")
    ledger = WorkLedger.open(run_path)
    ledger.append(kind="agent.claimed", task_id="T-resume", payload={})
    ledger.append(kind="task.started", task_id="T-resume", payload={})
    ledger.close()

    # Resume after the first entry, so the second is the only pre-existing row
    # the static render should show.
    code, output = _follow(traces_dir, "T-resume", "--since", "run-resume:0")
    assert code == 0, output
    assert "task.started" in output

    def _append_later() -> None:
        time.sleep(0.05)
        bg = WorkLedger.open(run_path)
        bg.append(kind="task.completed", task_id="T-resume", payload={"outcome": "ok"})
        bg.append(kind="run.closed", task_id="", payload={"status": "done"})
        bg.close()

    writer = threading.Thread(target=_append_later)
    writer.start()
    code, live_output = _follow(traces_dir, "T-resume", "--since", "run-resume:0", "--live", "--poll-interval", "0.01")
    writer.join()

    assert code == 0, live_output
    # The entry before the resume point must not be re-emitted by the loop.
    assert live_output.count("agent.claimed") == 0, live_output
    # The genuinely new entry still arrives.
    assert "task.completed" in live_output


def test_live_out_file_is_one_format_throughout(sdd_dir: Path, traces_dir: Path, tmp_path: Path) -> None:
    """A `--live --out` file is readable by one parser, whatever it is called.

    The format follows `--as-json` and `--live`, never the suffix. Text mode
    wrote a JSON array for a `.json` name and then appended plain-text rows to
    the same file, leaving a document no consumer could read.
    """
    run_path = _ledger_dir(sdd_dir, "run-fmt")
    ledger = WorkLedger.open(run_path)
    ledger.append(kind="agent.claimed", task_id="T-fmt", payload={})
    ledger.close()

    def _close_later() -> None:
        time.sleep(0.05)
        bg = WorkLedger.open(run_path)
        bg.append(kind="task.completed", task_id="T-fmt", payload={"outcome": "ok"})
        bg.append(kind="run.closed", task_id="", payload={"status": "done"})
        bg.close()

    # Text mode, JSON-looking name: stays text.
    out_txt = tmp_path / "live.json"
    t = threading.Thread(target=_close_later)
    t.start()
    code, _ = _follow(traces_dir, "T-fmt", "--live", "--poll-interval", "0.01", "--out", str(out_txt))
    t.join()
    assert code == 0
    body = out_txt.read_text(encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        json.loads(body)
    assert "agent.claimed" in body

    # JSON mode under --live: JSONL, every line parsing on its own.
    run2 = _ledger_dir(sdd_dir, "run-fmt2")
    l2 = WorkLedger.open(run2)
    l2.append(kind="agent.claimed", task_id="T-fmt2", payload={})
    l2.append(kind="run.closed", task_id="", payload={"status": "done"})
    l2.close()

    out_jsonl = tmp_path / "live.txt"
    code, _ = _follow(traces_dir, "T-fmt2", "--live", "--poll-interval", "0.01", "--as-json", "--out", str(out_jsonl))
    assert code == 0
    lines = [ln for ln in out_jsonl.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert lines, "expected at least one JSONL row"
    for ln in lines:
        json.loads(ln)
