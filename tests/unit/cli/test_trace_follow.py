"""`bernstein trace follow <entity-id>`: every trace entry that names one entity.

`trace show` globs `.sdd/traces/` for a task id and prints whichever file
matched, once. An entity id -- a task, a run, a grant -- appears across several
traces, and nothing joined them, so following one meant exporting the store and
grepping it (#5114).

The acceptance criterion that shapes the implementation is the third one: for a
finished run the output must be byte-identical across invocations. `reindex`
rebuilds `index.jsonl` by walking the blob tree, so index order is a filesystem
artefact; the ordering here is explicit and total.
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

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def store(tmp_path: Path) -> ContentAddressedTraceStore:
    """A store holding three traces for one task and one for another."""
    traces = tmp_path / ".sdd" / "traces"
    traces.mkdir(parents=True)
    store = ContentAddressedTraceStore(traces)
    # Written out of order on purpose: index order is a write/filesystem
    # artefact, and `follow` must not inherit it.
    for body, trace_id, task_id, started in (
        (b'{"event": "third"}', "trace-c", "T-100", 300.0),
        (b'{"event": "first"}', "trace-a", "T-100", 100.0),
        (b'{"event": "second"}', "trace-b", "T-100", 200.0),
        (b'{"event": "other"}', "trace-z", "T-999", 150.0),
    ):
        store.put(
            body,
            hints=TraceMetadataHints(trace_id=trace_id, task_id=task_id, started_at=started),
        )
    return store


def _follow(store: ContentAddressedTraceStore, *args: str) -> tuple[int, str]:
    result = CliRunner().invoke(trace_cmd, ["--traces-dir", str(store.root), "follow", *args])
    return result.exit_code, result.output


def test_follow_reports_every_trace_that_references_the_entity(
    store: ContentAddressedTraceStore,
) -> None:
    """The join `trace show` never did: one entity, all of its traces."""
    code, output = _follow(store, "T-100", "--as-json")
    assert code == 0
    entries = json.loads(output)
    assert [entry["trace_id"] for entry in entries] == ["trace-a", "trace-b", "trace-c"]


def test_entries_are_ordered_by_start_time_not_index_order(
    store: ContentAddressedTraceStore,
) -> None:
    """`trace-c` was written first and starts last; order follows the run."""
    _, output = _follow(store, "T-100", "--as-json")
    starts = [entry["started_at"] for entry in json.loads(output)]
    assert starts == sorted(starts)


def test_a_finished_run_prints_byte_identically_across_invocations(
    store: ContentAddressedTraceStore,
) -> None:
    """Acceptance criterion 3, and the reason ordering is explicit.

    Index order is a write-order artefact, so the ordering here is explicit
    and total: start time, then trace id. Nothing in the output is derived
    from the clock or the locale of the reader.

    Note this does NOT survive `store.reindex()`, but for a reason outside
    this command: `reindex` rebuilds from the blob tree alone and cannot
    recover hint-supplied metadata, so `trace_id` collapses to a hash prefix
    and `task_id`/`started_at` are lost. That is a store-level defect, not an
    ordering one, and is reported separately.
    """
    first_code, first = _follow(store, "T-100", "--as-json")
    second_code, second = _follow(store, "T-100", "--as-json")
    assert (first_code, second_code) == (0, 0)
    assert first == second
    assert first == _follow(store, "T-100", "--as-json")[1]


def test_a_trace_for_another_entity_is_not_reported(store: ContentAddressedTraceStore) -> None:
    """The filter has to actually filter, or `follow` is just `index`."""
    _, output = _follow(store, "T-100", "--as-json")
    assert all(entry["task_id"] == "T-100" for entry in json.loads(output))


def test_an_entity_named_by_trace_id_is_followed_too(store: ContentAddressedTraceStore) -> None:
    """An entity id reaches the index as a trace id as often as a task id."""
    code, output = _follow(store, "trace-z", "--as-json")
    assert code == 0
    assert [entry["trace_id"] for entry in json.loads(output)] == ["trace-z"]


def test_an_entity_with_no_traces_exits_non_zero(store: ContentAddressedTraceStore) -> None:
    """Silence would read as "this ran and produced nothing"."""
    code, output = _follow(store, "T-nothing")
    assert code == 1
    assert "No trace entries reference" in output


def test_a_missing_traces_directory_is_named(tmp_path: Path) -> None:
    """The other way `follow` can legitimately produce no rows."""
    result = CliRunner().invoke(trace_cmd, ["--traces-dir", str(tmp_path / "absent"), "follow", "T-100"])
    assert result.exit_code == 1
    assert "Traces directory not found" in result.output


def test_the_table_names_each_trace_and_its_task(store: ContentAddressedTraceStore) -> None:
    """The human form carries the same rows as `--as-json`."""
    code, output = _follow(store, "T-100")
    assert code == 0
    condensed = " ".join(output.split())
    assert "trace-a" in condensed
    assert "3 entries" in condensed


def test_timestamps_are_utc_so_output_does_not_follow_the_reader(
    store: ContentAddressedTraceStore,
) -> None:
    """A local-time render would break the byte-identical guarantee across hosts."""
    _, output = _follow(store, "trace-a")
    assert "1970-01-01T00:01:40Z" in " ".join(output.split())
