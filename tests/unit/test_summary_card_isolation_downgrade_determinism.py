"""The isolation-downgrade row does not depend on spawn scheduling (issue #3071).

``SpawnerCore._record_isolation_downgrade`` appends to a plain list from
whichever spawn thread hit the fallback. Which entry lands at index ``0`` is a
function of thread scheduling, and the summary card used to render
``isolation_downgrades[0]``. That was invisible while every downgrade in a run
carried the same ``(requested, actual)`` pair; #3028 added a second downgrade
kind, so one run can now hold two distinct pairs and the card can name either
of them for the same run.

The fix is in the display, not in the recording. Serialising the append under
the sandbox audit lock would make append-and-emit atomic, but it would not
change which entry a race puts first, so the card would stay scheduling
dependent. Choosing the representative pair by a total order over the entries
removes the dependency outright: any permutation of the same downgrades renders
the same line.

These tests fail on the positional implementation and pass on the ordered one.
"""

from __future__ import annotations

import itertools
from io import StringIO

from rich.console import Console

from bernstein.cli.display.summary_card import (
    RunSummaryData,
    _representative_downgrade,
    build_summary_card,
)


def _downgrade(session: str, requested: str, actual: str) -> dict[str, str]:
    return {
        "session_id": session,
        "requested": requested,
        "actual": actual,
        "reason": "probe",
    }


def _render(downgrades: list[dict[str, str]]) -> str:
    data = RunSummaryData(
        run_id="run-1",
        tasks_completed=2,
        tasks_total=2,
        tasks_failed=0,
        wall_clock_seconds=10.0,
        total_cost_usd=0.0,
        quality_score=None,
        isolation_downgrades=downgrades,
    )
    buf = StringIO()
    Console(file=buf, width=100, color_system=None).print(build_summary_card(data))
    return buf.getvalue()


def _downgrade_line(rendered: str) -> str:
    lines = [ln for ln in rendered.splitlines() if "Isolation downgrade" in ln]
    assert len(lines) == 1, f"expected exactly one downgrade row, got {lines}"
    return lines[0]


# ---------------------------------------------------------------------------
# The permutation axis
# ---------------------------------------------------------------------------


def test_two_downgrade_kinds_render_identically_in_either_order() -> None:
    """A heterogeneous run renders the same line whichever entry raced first."""
    container = _downgrade("sess-a", "container", "worktree")
    vm = _downgrade("sess-b", "vm", "container")

    first = _downgrade_line(_render([container, vm]))
    second = _downgrade_line(_render([vm, container]))

    assert first == second, f"downgrade row depends on list order:\n{first}\n{second}"


def test_every_permutation_of_a_mixed_run_renders_one_line() -> None:
    """Order independence holds across the whole permutation group, not one swap.

    Three entries, two kinds: six orderings, one rendered line. A rule that
    happened to be stable under a single swap but not under rotation would pass
    the two-entry case and fail here.
    """
    entries = [
        _downgrade("sess-a", "container", "worktree"),
        _downgrade("sess-b", "container", "worktree"),
        _downgrade("sess-c", "vm", "container"),
    ]
    rendered = {_downgrade_line(_render(list(perm))) for perm in itertools.permutations(entries)}
    assert len(rendered) == 1, f"downgrade row varies across permutations: {sorted(rendered)}"


def test_homogeneous_run_still_names_the_pair_and_the_total() -> None:
    """The single-kind case, which is the common one, keeps its wording."""
    entries = [_downgrade(f"sess-{i}", "container", "worktree") for i in range(3)]
    line = _downgrade_line(_render(entries))
    assert "container -> worktree" in line
    assert "x3" in line


def test_mixed_run_reports_the_representative_count_not_the_total() -> None:
    """A heterogeneous run must not attribute every downgrade to one pair.

    Rendering ``requested -> actual (xN)`` with ``N`` the total says three
    spawns fell back the way the named pair did, which is false when only two
    of them did.
    """
    entries = [
        _downgrade("sess-a", "container", "worktree"),
        _downgrade("sess-b", "container", "worktree"),
        _downgrade("sess-c", "vm", "container"),
    ]
    line = _downgrade_line(_render(entries))
    assert "container -> worktree" in line
    assert "2 of 3" in line
    assert "2 kinds" in line


# ---------------------------------------------------------------------------
# The selection rule itself
# ---------------------------------------------------------------------------


def test_representative_is_the_most_frequent_pair() -> None:
    entries = [
        _downgrade("a", "vm", "container"),
        _downgrade("b", "container", "worktree"),
        _downgrade("c", "container", "worktree"),
    ]
    assert _representative_downgrade(entries) == ("container", "worktree", 2, 2)


def test_equal_frequency_breaks_the_tie_lexicographically() -> None:
    """A tie must resolve on the pair itself, never on arrival order."""
    vm = _downgrade("a", "vm", "container")
    container = _downgrade("b", "container", "worktree")
    assert _representative_downgrade([vm, container]) == _representative_downgrade([container, vm])
    assert _representative_downgrade([vm, container]) == ("container", "worktree", 1, 2)


def test_missing_fields_fall_back_to_the_documented_defaults() -> None:
    """An entry without the keys still resolves, as the positional read did."""
    assert _representative_downgrade([{"session_id": "a"}]) == ("container", "worktree", 1, 1)


def test_empty_input_is_not_reachable_from_the_card_but_is_still_total() -> None:
    """The helper is total: the card guards on truthiness, the helper does not rely on it."""
    assert _representative_downgrade([]) == ("container", "worktree", 0, 0)
