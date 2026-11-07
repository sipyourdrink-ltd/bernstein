"""Cost per verdict in bundles, and cost deltas in `bench compare` (#5464).

Bundles reported verdicts and nothing about what producing them cost, so two
bundles could be compared on score and not on money. The tricky part is not
recording the numbers, it is recording them without breaking every bundle that
already exists: `SubmissionBundle.from_dict` recomputes `bundle_hash` and
refuses a mismatch with "the bundle file may have been tampered with", and the
hash payload includes each `TaskResult.to_dict()`. An unconditional new key
would therefore accuse every previously valid bundle of forgery.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from bernstein.eval.bench.bundle import SubmissionBundle, TaskCost, TaskResult

if TYPE_CHECKING:
    from pathlib import Path


def _bundle(*costs: TaskCost | None) -> SubmissionBundle:
    return SubmissionBundle(
        suite_hash="s" * 64,
        suite_version="v1",
        task_results=[
            TaskResult(
                task_id=f"t{i}",
                task_hash="h" * 64,
                receipt={"journal_head": f"j{i}"},
                passed=True,
                score=1.0,
                cost=cost,
            )
            for i, cost in enumerate(costs)
        ],
        scheduler_config={"workers": 1},
        submitted_at="2026-01-01T00:00:00Z",
    )


# --- the bundle schema -----------------------------------------------------


def test_a_task_records_what_its_verdict_cost(tmp_path: Path) -> None:
    bundle = _bundle(TaskCost(tokens=1200, cost_usd=0.034, wall_time_s=12.5))
    bundle.save(tmp_path / "b.json")

    back = SubmissionBundle.load(tmp_path / "b.json")
    assert back.task_results[0].cost == TaskCost(tokens=1200, cost_usd=0.034, wall_time_s=12.5)


def test_suite_totals_are_the_sum_of_the_tasks_that_measured(tmp_path: Path) -> None:
    """A partially instrumented run reports what it knows, and says how much that was."""
    bundle = _bundle(
        TaskCost(tokens=1000, cost_usd=0.02, wall_time_s=10.0),
        None,
        TaskCost(tokens=500, cost_usd=0.01, wall_time_s=5.0),
    )
    total = bundle.total_cost
    assert total is not None
    assert (total.tokens, total.cost_usd, total.wall_time_s) == (1500, 0.03, 15.0)
    assert bundle.measured_tasks == 2, "three tasks, two of them measured"


def test_cost_per_verdict_divides_by_the_tasks_that_were_measured(tmp_path: Path) -> None:
    """Not by every task in the suite.

    Mixing the two denominators is how a partially instrumented run reports a
    cost per verdict lower than any verdict actually cost.
    """
    bundle = _bundle(TaskCost(cost_usd=0.02), None, TaskCost(cost_usd=0.04))
    assert bundle.cost_per_verdict == 0.03


def test_an_unmeasured_run_reports_no_cost_rather_than_zero(tmp_path: Path) -> None:
    """A run that did not measure and a run that was free are different facts."""
    bundle = _bundle(None, None)
    assert bundle.total_cost is None
    assert bundle.cost_per_verdict is None
    assert bundle.measured_tasks == 0

    bundle.save(tmp_path / "b.json")
    raw = json.loads((tmp_path / "b.json").read_text(encoding="utf-8"))
    assert "total_cost" not in raw, "an absent measurement must not serialise as $0.00"


# --- the compatibility constraint, which is the hard part ------------------


def test_a_bundle_written_before_costs_existed_still_loads(tmp_path: Path) -> None:
    """The failure an additive change would otherwise cause.

    `from_dict` recomputes `bundle_hash` over a payload that includes every
    `TaskResult.to_dict()`. A `"cost": null` emitted unconditionally would
    change the canonical bytes of every bundle ever written, so each would
    recompute to a different hash and be refused as tampered with.
    """
    legacy = _bundle(None)
    legacy.save(tmp_path / "legacy.json")

    raw = json.loads((tmp_path / "legacy.json").read_text(encoding="utf-8"))
    assert "cost" not in raw["task_results"][0], "an unmeasured cost must not reach the hash payload"

    reloaded = SubmissionBundle.load(tmp_path / "legacy.json")
    assert reloaded.task_results[0].cost is None
    assert reloaded.bundle_hash() == raw["bundle_hash"]


def test_a_recorded_cost_is_committed_to_the_bundle_hash(tmp_path: Path) -> None:
    """The other half: once measured, the numbers are evidence and are sealed.

    Omitting them from the hash to keep it stable would leave the cost fields
    editable after signing, which is the shape of every claim this bundle
    format exists to prevent.
    """
    free = _bundle(TaskCost(tokens=1, cost_usd=0.01, wall_time_s=1.0))
    dear = _bundle(TaskCost(tokens=9, cost_usd=9.99, wall_time_s=9.0))
    assert free.bundle_hash() != dear.bundle_hash()

    # And editing the cost after the fact is caught on load.
    free.save(tmp_path / "b.json")
    raw = json.loads((tmp_path / "b.json").read_text(encoding="utf-8"))
    raw["task_results"][0]["cost"]["cost_usd"] = 0.0
    (tmp_path / "tampered.json").write_text(json.dumps(raw), encoding="utf-8")

    try:
        SubmissionBundle.load(tmp_path / "tampered.json")
    except ValueError as exc:
        assert "hash mismatch" in str(exc).lower()
    else:  # pragma: no cover - the assertion above is the point
        raise AssertionError("an edited cost was accepted")


def test_derived_totals_are_not_part_of_the_hash(tmp_path: Path) -> None:
    """They are read off `task_results`, like `pass_rate` already is.

    Hashing a derived field lets a bundle disagree with itself: the stored
    total and the sum of the rows would both be committed, and nothing says
    which one a verifier should believe.
    """
    bundle = _bundle(TaskCost(cost_usd=0.02))
    bundle.save(tmp_path / "b.json")
    raw = json.loads((tmp_path / "b.json").read_text(encoding="utf-8"))
    assert "total_cost" in raw, "the total is serialised for a reader"

    raw["total_cost"]["cost_usd"] = 999.0
    (tmp_path / "edited.json").write_text(json.dumps(raw), encoding="utf-8")
    # Loads fine — the total is not evidence, the rows are — and the rebuilt
    # bundle reports the truth from the rows rather than the edited total.
    reloaded = SubmissionBundle.load(tmp_path / "edited.json")
    assert reloaded.total_cost is not None
    assert reloaded.total_cost.cost_usd == 0.02


# --- bench compare ---------------------------------------------------------


def _saved(tmp_path: Path, name: str, *costs: TaskCost | None) -> Path:
    from bernstein.eval.bench.bundle import harness_fingerprint

    bundle = _bundle(*costs)
    bundle.harness_fingerprint = harness_fingerprint(bundle.scheduler_config)
    path = tmp_path / name
    bundle.save(path)
    return path


def test_compare_shows_cost_deltas_beside_the_score(tmp_path: Path) -> None:
    from click.testing import CliRunner

    from bernstein.eval.bench.bench_cli import bench_group

    a = _saved(tmp_path, "a.json", TaskCost(tokens=1000, cost_usd=0.0200, wall_time_s=10.0))
    b = _saved(tmp_path, "b.json", TaskCost(tokens=1500, cost_usd=0.0350, wall_time_s=14.0))

    result = CliRunner().invoke(bench_group, ["compare", str(a), str(b)])
    assert result.exit_code == 0, result.output
    assert "cost delta" in result.output
    assert "+$0.0150" in result.output, result.output
    assert "+500" in result.output, "token delta"
    assert "cost per verdict" in result.output


def test_compare_says_cost_is_unrecorded_rather_than_printing_nothing(tmp_path: Path) -> None:
    """Silence reads as "no difference" to someone comparing two runs on cost."""
    from click.testing import CliRunner

    from bernstein.eval.bench.bench_cli import bench_group

    a = _saved(tmp_path, "a.json", TaskCost(cost_usd=0.02))
    b = _saved(tmp_path, "b.json", None)

    result = CliRunner().invoke(bench_group, ["compare", str(a), str(b)])
    assert result.exit_code == 0, result.output
    assert "not recorded in b.json" in result.output


def test_compare_deltas_are_b_relative_to_a_whichever_bundle_ranks_first(tmp_path: Path) -> None:
    """A sign that flips with the ranking is a number nobody can act on.

    Here B is cheaper, and B also outranks A on nothing — the scores are equal
    — so the ordering above is not what decides the sign.
    """
    from click.testing import CliRunner

    from bernstein.eval.bench.bench_cli import bench_group

    a = _saved(tmp_path, "a.json", TaskCost(cost_usd=0.0500))
    b = _saved(tmp_path, "b.json", TaskCost(cost_usd=0.0100))

    result = CliRunner().invoke(bench_group, ["compare", str(a), str(b)])
    assert result.exit_code == 0, result.output
    assert "-$0.0400" in result.output, result.output
