"""A crashed benchmark run can resume from the results it already wrote.

`ResultStore` is the resume point: `harness.py` calls `already_evaluated` per
instance under the comment "Resume support: skip already-evaluated instances".
Appending is not atomic, so a process killed mid-write leaves a partial last
line -- and a bare `json.loads` over every line made that one fragment poison
the whole file. `already_evaluated` raised, so the run could not resume, and
every instance it HAD completed was re-evaluated. On SWE-Bench that is a real
model call and real money, paid again for work already done.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from benchmarks.swe_bench.metrics import InstanceResult, ResultStore

if TYPE_CHECKING:
    from pathlib import Path


def _result(instance_id: str) -> InstanceResult:
    return InstanceResult(
        instance_id=instance_id,
        scenario_name="solo",
        status="resolved",
        resolved=True,
        wall_time_s=1.0,
        total_tokens=10,
        total_cost_usd=0.01,
    )


def _tear(path: Path) -> None:
    """Append a fragment, the way an interrupted write leaves one."""
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"instance_id": "i-3", "scenario_na')


def test_a_torn_final_line_does_not_cost_the_results_before_it(tmp_path: Path) -> None:
    store = ResultStore(tmp_path)
    store.append(_result("i-1"))
    store.append(_result("i-2"))
    _tear(tmp_path / "solo.jsonl")

    assert [r.instance_id for r in store.load("solo")] == ["i-1", "i-2"]


def test_a_crashed_run_can_still_resume(tmp_path: Path) -> None:
    """The property the store exists for, asserted through the call the harness makes."""
    store = ResultStore(tmp_path)
    store.append(_result("i-1"))
    _tear(tmp_path / "solo.jsonl")

    assert store.already_evaluated("solo", "i-1") is True, "completed work must not be re-run"
    assert store.already_evaluated("solo", "i-3") is False, "the torn instance did not finish"


def test_corruption_in_the_middle_is_an_error_rather_than_a_silent_drop(tmp_path: Path) -> None:
    """Skipping every unparseable line would trade one failure for a worse one.

    Only the last line can be torn by an interrupted append. A bad line
    anywhere else means something went wrong that nobody would otherwise hear
    about, and dropping it loses a completed result silently.
    """
    store = ResultStore(tmp_path)
    store.append(_result("i-1"))
    good = (tmp_path / "solo.jsonl").read_text(encoding="utf-8").splitlines()[0]
    (tmp_path / "solo.jsonl").write_text(f'{{"torn\n{good}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="not the last line"):
        store.load("solo")


def test_a_blank_trailing_line_is_not_a_tear(tmp_path: Path) -> None:
    """Every well-formed file ends with a newline, so this is the common case."""
    store = ResultStore(tmp_path)
    store.append(_result("i-1"))
    with (tmp_path / "solo.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("\n\n")

    assert [r.instance_id for r in store.load("solo")] == ["i-1"]


def test_an_intact_file_round_trips_unchanged(tmp_path: Path) -> None:
    """The tolerance must not change the ordinary path."""
    store = ResultStore(tmp_path)
    for name in ("i-1", "i-2", "i-3"):
        store.append(_result(name))

    assert [r.instance_id for r in store.load("solo")] == ["i-1", "i-2", "i-3"]
    assert store.already_evaluated("solo", "i-2") is True


def test_an_appended_result_is_on_disk_before_append_returns(tmp_path: Path) -> None:
    """`fsync`, because a finished instance held in the page cache is one paid for twice.

    Asserted by reading through a second store object, which shares no buffer
    with the first.
    """
    ResultStore(tmp_path).append(_result("i-1"))
    assert [r.instance_id for r in ResultStore(tmp_path).load("solo")] == ["i-1"]
