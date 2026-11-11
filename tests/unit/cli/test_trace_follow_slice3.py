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


class TestFollowHardening:
    """The five issues from the #6397 second review."""

    def test_entity_id_cannot_walk_out_of_the_ledger_root(self, sdd_dir: Path) -> None:
        """ENTITY_ID is argv, so a traversal must not reach outside the root.

        Tested against `_is_entity_terminal` directly rather than through the
        CLI: the command exits early on "no entries" whatever the id, so a
        CLI-level assertion passes with or without the containment join and
        proves nothing. This reaches the join itself.
        """
        from bernstein.cli.commands.advanced_cmd import _is_entity_terminal

        # A directory the traversal would reach if the join were raw, holding a
        # ledger whose run.closed would otherwise be read as this entity's.
        outside = sdd_dir / "runtime" / "escaped"
        outside.mkdir(parents=True)
        victim = WorkLedger.open(outside)
        victim.append(kind="run.closed", task_id="", payload={"status": "done"})
        victim.close()
        (sdd_dir / "runtime" / "ledger").mkdir(parents=True, exist_ok=True)

        # ../escaped resolves outside <sdd>/runtime/ledger. Raw joining would
        # find the directory and report the entity terminal from someone
        # else's ledger; the contained join refuses the name outright.
        assert _is_entity_terminal(sdd_dir, "../escaped", []) is False

    def test_live_with_no_entries_exits_instead_of_polling_forever(self, sdd_dir: Path, traces_dir: Path) -> None:
        """With nothing in the ledger the terminal check can never fire."""
        code, output = _follow(traces_dir, "T-nothing", "--live", "--poll-interval", "0.01")
        assert code == 1
        assert "No trace entries reference" in output

    def test_poll_interval_has_a_floor(self, traces_dir: Path) -> None:
        """0 turns the follow loop into a busy spin."""
        code, output = _follow(traces_dir, "T-x", "--live", "--poll-interval", "0")
        assert code != 0
        assert "0.01" in output or "not in the range" in output.lower()

    def test_as_json_live_stdout_is_jsonl_throughout(self, sdd_dir: Path, traces_dir: Path) -> None:
        """stdout follows the same rule as the file: no array then loose objects."""
        run_path = _ledger_dir(sdd_dir, "run-sj")
        ledger = WorkLedger.open(run_path)
        ledger.append(kind="agent.claimed", task_id="T-sj", payload={})
        ledger.append(kind="run.closed", task_id="", payload={"status": "done"})
        ledger.close()

        code, output = _follow(traces_dir, "T-sj", "--live", "--as-json", "--poll-interval", "0.01")
        assert code == 0
        payload = [ln for ln in output.splitlines() if ln.strip().startswith("{")]
        assert payload, output
        for line in payload:
            json.loads(line)
        assert payload[0].startswith("{"), output.splitlines()[:3]

    @pytest.mark.parametrize("trailing_newline", [True, False])
    def test_jsonl_resume_preserves_existing_parseable_capture(
        self, sdd_dir: Path, traces_dir: Path, tmp_path: Path, trailing_newline: bool
    ) -> None:
        """A continuation appends JSON objects to an existing JSONL capture."""
        ledger = WorkLedger.open(_ledger_dir(sdd_dir, "run-ap"))
        ledger.append(kind="task.started", task_id="T-ap", payload={})
        ledger.append(kind="run.closed", task_id="", payload={})
        ledger.close()

        out = tmp_path / "capture.jsonl"
        code, _ = _follow(traces_dir, "T-ap", "--as-json", "--live", "--out", str(out))
        assert code == 0
        first = out.read_text(encoding="utf-8")
        if not trailing_newline:
            first = first.rstrip("\n")
            out.write_text(first, encoding="utf-8")
        assert len([json.loads(line) for line in first.splitlines()]) == 1

        ContentAddressedTraceStore(traces_dir).put(
            b'{"event": "later"}',
            hints=TraceMetadataHints(trace_id="later", task_id="T-ap", started_at=4_000_000_000.0),
        )
        code, _ = _follow(traces_dir, "T-ap", "--since", "run-ap:0", "--as-json", "--out", str(out))
        assert code == 0
        second = out.read_text(encoding="utf-8")
        assert second.startswith(first)
        rows = [json.loads(line) for line in second.splitlines()]
        assert len(rows) == 2
        assert rows[-1]["trace_id"] == "later"

    def test_json_resume_refuses_a_one_shot_array_without_modifying_it(
        self, sdd_dir: Path, traces_dir: Path, tmp_path: Path
    ) -> None:
        """An array capture must not become an array followed by loose objects."""
        ledger = WorkLedger.open(_ledger_dir(sdd_dir, "run-array"))
        ledger.append(kind="task.started", task_id="T-array", payload={})
        ledger.append(kind="task.completed", task_id="T-array", payload={})
        ledger.close()
        out = tmp_path / "capture.json"
        code, _ = _follow(traces_dir, "T-array", "--as-json", "--out", str(out))
        assert code == 0
        first = out.read_bytes()
        assert len(json.loads(first)) == 2

        code, output = _follow(traces_dir, "T-array", "--since", "run-array:0", "--as-json", "--out", str(out))
        assert code == 1, output
        assert "one-shot JSON array" in output
        assert out.read_bytes() == first
        assert len(json.loads(out.read_bytes())) == 2


@pytest.mark.parametrize("ending", ["closed", "interrupt"])
def test_active_json_follow_keeps_notices_out_of_stdout(
    sdd_dir: Path, traces_dir: Path, monkeypatch: pytest.MonkeyPatch, ending: str
) -> None:
    """Every nonempty stdout line remains a JSON object during active follow."""
    run_path = _ledger_dir(sdd_dir, "run-notices")
    ledger = WorkLedger.open(run_path)
    ledger.append(kind="task.started", task_id="T-notices", payload={"items": [1, 2]})
    ledger.close()

    def finish_on_poll(_interval: float) -> None:
        if ending == "interrupt":
            raise KeyboardInterrupt
        with WorkLedger.open(run_path) as writer:
            writer.append(kind="run.closed", task_id="", payload={})

    monkeypatch.setattr("bernstein.cli.commands.advanced_cmd.time.sleep", finish_on_poll)
    result = CliRunner().invoke(
        trace_cmd, ["--traces-dir", str(traces_dir), "follow", "T-notices", "--as-json", "--live"]
    )
    assert result.exit_code == 0, result.output
    rows = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert rows and all(isinstance(row, dict) for row in rows)
    assert "following" in result.stderr
    assert ("run terminated." if ending == "closed" else "stopped.") in result.stderr


@pytest.mark.parametrize("old_ending", ["run.closed", "task.completed", "task.failed", "task.abandoned"])
def test_a_historical_task_occurrence_cannot_close_an_active_occurrence(
    sdd_dir: Path, traces_dir: Path, old_ending: str
) -> None:
    """Task ids span runs in the collector, so every occurrence must finish."""
    from bernstein.cli.commands.advanced_cmd import _is_entity_terminal, _ledger_entries_for_entity

    with WorkLedger.open(_ledger_dir(sdd_dir, "old")) as ledger:
        ledger.append(kind="task.started", task_id="T-reused", payload={})
        ledger.append(kind=old_ending, task_id="" if old_ending == "run.closed" else "T-reused", payload={})
    current = _ledger_dir(sdd_dir, "current")
    with WorkLedger.open(current) as ledger:
        ledger.append(kind="task.started", task_id="T-reused", payload={})

    assert not _is_entity_terminal(sdd_dir, "T-reused", _ledger_entries_for_entity(sdd_dir, "T-reused"))
    with WorkLedger.open(current) as ledger:
        ledger.append(kind="task.completed", task_id="T-reused", payload={})
    assert _is_entity_terminal(sdd_dir, "T-reused", _ledger_entries_for_entity(sdd_dir, "T-reused"))


def test_live_resume_keeps_the_prior_unterminated_record_parseable(
    sdd_dir: Path, traces_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_path = _ledger_dir(sdd_dir, "run-unterminated")
    ledger = WorkLedger.open(run_path)
    ledger.append(kind="task.started", task_id="T-unterminated", payload={})
    ledger.close()
    code, output = _follow(traces_dir, "T-unterminated", "--as-json")
    assert code == 0, output
    prior = json.dumps(json.loads(output)[0])
    out = tmp_path / "capture.jsonl"
    out.write_text(prior, encoding="utf-8")

    def complete_task(_delay: float) -> None:
        writer = WorkLedger.open(run_path)
        writer.append(kind="task.completed", task_id="T-unterminated", payload={})
        writer.close()

    monkeypatch.setattr("bernstein.cli.commands.advanced_cmd.time.sleep", complete_task)
    code, output = _follow(
        traces_dir, "T-unterminated", "--as-json", "--since", "run-unterminated:0", "--live", "--out", str(out)
    )
    assert code == 0, output
    capture = out.read_text(encoding="utf-8")
    assert capture.startswith(prior)
    rows = [json.loads(line) for line in capture.splitlines()]
    assert len(rows) == 2
    assert rows[0]["kind"] == "task.started"
    assert rows[1]["kind"] == "task.completed"


@pytest.mark.parametrize("capture", [b"old text capture\n", b'{"old": true}\nbroken\n', b"\xff\n"])
def test_json_resume_rejects_incompatible_capture_without_writing(
    sdd_dir: Path, traces_dir: Path, tmp_path: Path, capture: bytes
) -> None:
    """A continuation cannot silently append JSONL to a different format."""
    with WorkLedger.open(_ledger_dir(sdd_dir, "run-invalid")) as ledger:
        ledger.append(kind="task.started", task_id="T-invalid", payload={})
        ledger.append(kind="task.completed", task_id="T-invalid", payload={})
    out = tmp_path / "capture"
    out.write_bytes(capture)

    code, output = _follow(traces_dir, "T-invalid", "--since", "run-invalid:0", "--as-json", "--out", str(out))
    assert code == 1, output
    assert "JSONL" in output
    assert out.read_bytes() == capture


def test_live_cli_waits_for_the_active_run_of_a_reused_task_id(sdd_dir: Path, traces_dir: Path, tmp_path: Path) -> None:
    """A separate CLI process follows fresh ledger writes after historical closure."""
    import os
    import queue
    import subprocess
    import sys
    from pathlib import Path

    with WorkLedger.open(_ledger_dir(sdd_dir, "old-process")) as ledger:
        ledger.append(kind="task.started", task_id="T-process", payload={})
        ledger.append(kind="run.closed", task_id="", payload={})
    current = _ledger_dir(sdd_dir, "active-process")
    with WorkLedger.open(current) as ledger:
        ledger.append(kind="task.started", task_id="T-process", payload={})
    env = dict(os.environ)
    env.pop("BERNSTEIN_TELEMETRY_DSN", None)
    root = Path(__file__).resolve().parents[3]
    env["PYTHONPATH"] = os.pathsep.join([str(root / "src"), str(root)])
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "bernstein",
            "trace",
            "--traces-dir",
            str(traces_dir),
            "follow",
            "T-process",
            "--since",
            "active-process:0",
            "--as-json",
            "--live",
            "--poll-interval",
            "0.01",
        ],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stderr_stream = process.stderr
    assert stderr_stream is not None
    notice: queue.Queue[str] = queue.Queue()
    reader = threading.Thread(target=lambda: notice.put(stderr_stream.readline()), daemon=True)
    reader.start()
    try:
        # Wait for actual loop entry, not a timing guess: closing before CLI
        # startup would let the historical-terminal bug pass accidentally.
        assert "following" in notice.get(timeout=15)
        reader.join(timeout=1)
        with WorkLedger.open(current) as ledger:
            ledger.append(kind="task.completed", task_id="T-process", payload={})
            ledger.append(kind="run.closed", task_id="", payload={})
        stdout, stderr = process.communicate(timeout=15)
        assert process.returncode == 0, stderr
        rows = [json.loads(line) for line in stdout.splitlines() if line.strip()]
        assert [row["kind"] for row in rows] == ["task.completed"]
        assert all(row["run_id"] == "active-process" for row in rows)
        assert "run terminated." in stderr
    finally:
        if process.poll() is None:
            process.kill()  # Only the subprocess created by this test.
            process.communicate(timeout=5)
        reader.join(timeout=1)
