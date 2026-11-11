"""`bernstein trace follow <entity-id>`: the cross-source join (#5114 slice 2).

Slice 1 (`tests/unit/cli/test_trace_follow.py`) covers `follow` over the trace
store alone. An entity id -- a task, a run, a grant -- is meaningful across
more than the trace store: the work ledger records the same run's task-graph
transitions under the same ids. This extends `follow` to also read the work
ledger and merge its entries into the same ordered output, so "which of these
journals mention this entity" stays one command instead of turning back into
export-and-grep for every journal beyond the first.
"""

from __future__ import annotations

import json
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


def test_follow_merges_ledger_entries_naming_the_same_task_id(sdd_dir: Path, traces_dir: Path) -> None:
    store = ContentAddressedTraceStore(traces_dir)
    store.put(b'{"event": "trace"}', hints=TraceMetadataHints(trace_id="trace-a", task_id="T-100", started_at=100.0))

    ledger = WorkLedger.open(_ledger_dir(sdd_dir, "run-1"))
    ledger.append(kind="agent.claimed", task_id="T-100", payload={"note": "claimed"})
    ledger.close()

    code, output = _follow(traces_dir, "T-100", "--as-json")
    assert code == 0
    rows = json.loads(output)
    sources = {row["source"] for row in rows}
    assert sources == {"trace", "ledger"}
    ledger_row = next(row for row in rows if row["source"] == "ledger")
    assert ledger_row["task_id"] == "T-100"
    assert ledger_row["kind"] == "agent.claimed"
    assert ledger_row["run_id"] == "run-1"


def test_follow_finds_ledger_entries_by_run_id_even_without_a_matching_task(sdd_dir: Path, traces_dir: Path) -> None:
    """The entity id can name the run (the ledger directory) rather than a task inside it."""
    ledger = WorkLedger.open(_ledger_dir(sdd_dir, "run-42"))
    ledger.append(kind="agent.claimed", task_id="T-1", payload={})
    ledger.append(kind="agent.completed", task_id="T-2", payload={})
    ledger.close()

    code, output = _follow(traces_dir, "run-42", "--as-json")
    assert code == 0
    rows = json.loads(output)
    assert {row["task_id"] for row in rows} == {"T-1", "T-2"}
    assert all(row["run_id"] == "run-42" for row in rows)


def test_merged_output_is_ordered_by_timestamp_across_sources(sdd_dir: Path, traces_dir: Path) -> None:
    store = ContentAddressedTraceStore(traces_dir)
    store.put(b'{"event": "late"}', hints=TraceMetadataHints(trace_id="trace-late", task_id="T-5", started_at=300.0))

    run_dir = _ledger_dir(sdd_dir, "run-9")
    ledger = WorkLedger.open(run_dir)
    ledger.append(kind="agent.claimed", task_id="T-5", payload={})
    ledger.close()
    # Force the ledger entry's timestamp earlier than the trace's, so a
    # source-blind (rather than timestamp-blind) sort would get this wrong.
    bucket = run_dir / "000000.jsonl"
    text = bucket.read_text(encoding="utf-8")
    row = json.loads(text.strip())
    row["ts"] = 50.0
    bucket.write_text(json.dumps(row) + "\n", encoding="utf-8")

    code, output = _follow(traces_dir, "T-5", "--as-json")
    assert code == 0
    rows = json.loads(output)
    assert [row["source"] for row in rows] == ["ledger", "trace"]


def test_an_entity_with_only_ledger_entries_and_no_traces_still_succeeds(sdd_dir: Path, traces_dir: Path) -> None:
    ledger = WorkLedger.open(_ledger_dir(sdd_dir, "run-only"))
    ledger.append(kind="agent.claimed", task_id="T-lonely", payload={})
    ledger.close()

    code, output = _follow(traces_dir, "T-lonely", "--as-json")
    assert code == 0
    rows = json.loads(output)
    assert len(rows) == 1
    assert rows[0]["source"] == "ledger"


def test_follow_is_read_only_and_never_creates_a_ledger_root(traces_dir: Path, sdd_dir: Path) -> None:
    """A query that finds nothing must not create `.sdd/runtime/ledger/`."""
    code, _ = _follow(traces_dir, "nothing-anywhere")
    assert code == 1
    assert not (sdd_dir / "runtime" / "ledger").exists()


def test_human_readable_output_names_both_sources_when_both_have_matches(sdd_dir: Path, traces_dir: Path) -> None:
    store = ContentAddressedTraceStore(traces_dir)
    store.put(b'{"event": "x"}', hints=TraceMetadataHints(trace_id="trace-a", task_id="T-7", started_at=1.0))
    ledger = WorkLedger.open(_ledger_dir(sdd_dir, "run-7"))
    ledger.append(kind="agent.claimed", task_id="T-7", payload={})
    ledger.close()

    code, output = _follow(traces_dir, "T-7")
    assert code == 0
    condensed = " ".join(output.split())
    assert "Traces referencing T-7" in condensed
    assert "Ledger entries referencing T-7" in condensed
    assert "1 trace entry, 1 ledger entry" in condensed
