"""Unit tests for the total-coverage monotonic ratchet.

Covers ``scripts/coverage_ratchet.py``:

- parsing total line coverage from a Cobertura ``coverage.xml``;
- the compare/bump decision (drop -> fail, rise -> pass + high-water bump,
  flat -> pass + no write);
- atomic baseline read/write (no partial file on crash);
- the weekly diff-coverage floor increment with its cap;
- graceful handling of a malformed / missing ``coverage.xml``.

The script is import-only at module level (no side effects) so these
tests can drive its functions directly without spawning a subprocess.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

# ``scripts/`` is not an installed package, so load the module by path.
_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "coverage_ratchet.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("coverage_ratchet", _SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register so dataclasses / typing resolve a stable module identity.
    sys.modules["coverage_ratchet"] = module
    spec.loader.exec_module(module)
    return module


ratchet = _load_module()


# --------------------------------------------------------------------------- #
# coverage.xml parsing
# --------------------------------------------------------------------------- #


def _write_coverage_xml(path: Path, line_rate: str) -> None:
    """Write a minimal Cobertura coverage.xml with the given root line-rate."""
    path.write_text(
        f'<?xml version="1.0" ?>\n'
        f'<coverage line-rate="{line_rate}" branch-rate="0.1" version="7.0">\n'
        f"  <packages></packages>\n"
        f"</coverage>\n",
        encoding="utf-8",
    )


def test_parse_total_coverage_reads_root_line_rate(tmp_path: Path) -> None:
    xml = tmp_path / "coverage.xml"
    _write_coverage_xml(xml, "0.1753")

    pct = ratchet.parse_total_coverage(xml)

    assert pct == pytest.approx(17.53, abs=0.001)


def test_parse_total_coverage_full_coverage(tmp_path: Path) -> None:
    xml = tmp_path / "coverage.xml"
    _write_coverage_xml(xml, "1.0")

    assert ratchet.parse_total_coverage(xml) == pytest.approx(100.0, abs=0.001)


def test_parse_total_coverage_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ratchet.CoverageParseError):
        ratchet.parse_total_coverage(tmp_path / "does-not-exist.xml")


def test_parse_total_coverage_malformed_xml_raises(tmp_path: Path) -> None:
    xml = tmp_path / "coverage.xml"
    xml.write_text("<coverage line-rate=", encoding="utf-8")  # truncated, invalid

    with pytest.raises(ratchet.CoverageParseError):
        ratchet.parse_total_coverage(xml)


def test_parse_total_coverage_missing_attribute_raises(tmp_path: Path) -> None:
    xml = tmp_path / "coverage.xml"
    xml.write_text('<?xml version="1.0" ?>\n<coverage version="7.0"></coverage>\n', encoding="utf-8")

    with pytest.raises(ratchet.CoverageParseError):
        ratchet.parse_total_coverage(xml)


def test_parse_total_coverage_non_numeric_attribute_raises(tmp_path: Path) -> None:
    xml = tmp_path / "coverage.xml"
    _write_coverage_xml(xml, "not-a-number")

    with pytest.raises(ratchet.CoverageParseError):
        ratchet.parse_total_coverage(xml)


def test_parse_total_coverage_empty_tree_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A parse that yields no root element is reported, not an AttributeError."""
    xml = tmp_path / "coverage.xml"
    _write_coverage_xml(xml, "0.5")

    class _RootlessTree:
        def getroot(self) -> None:
            return None

    monkeypatch.setattr(ratchet.ET, "parse", lambda _path: _RootlessTree())

    with pytest.raises(ratchet.CoverageParseError):
        ratchet.parse_total_coverage(xml)


# --------------------------------------------------------------------------- #
# baseline read / write
# --------------------------------------------------------------------------- #


def test_read_baseline_round_trips(tmp_path: Path) -> None:
    baseline_path = tmp_path / ".coverage-baseline.json"
    written = ratchet.Baseline(total_coverage_percent=17.5, diff_coverage_floor_percent=80)
    ratchet.write_baseline(baseline_path, written)

    loaded = ratchet.read_baseline(baseline_path)

    assert loaded.total_coverage_percent == pytest.approx(17.5)
    assert loaded.diff_coverage_floor_percent == 80


def test_read_baseline_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        ratchet.read_baseline(tmp_path / "nope.json")


def test_write_baseline_is_atomic_no_partial_temp_left(tmp_path: Path) -> None:
    """A successful write leaves exactly the target file, no temp turds."""
    baseline_path = tmp_path / ".coverage-baseline.json"
    ratchet.write_baseline(
        baseline_path,
        ratchet.Baseline(total_coverage_percent=20.0, diff_coverage_floor_percent=85),
    )

    siblings = list(tmp_path.iterdir())
    assert siblings == [baseline_path], f"unexpected leftover files: {siblings}"
    # File is valid JSON with the documented keys.
    data = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert set(data) >= {"total_coverage_percent", "diff_coverage_floor_percent"}


def test_write_baseline_does_not_corrupt_existing_on_serialise_failure(tmp_path: Path) -> None:
    """If serialisation blows up mid-write, the prior baseline survives."""
    baseline_path = tmp_path / ".coverage-baseline.json"
    good = ratchet.Baseline(total_coverage_percent=17.5, diff_coverage_floor_percent=80)
    ratchet.write_baseline(baseline_path, good)
    original_bytes = baseline_path.read_bytes()

    class _Unserialisable:
        pass

    bad = ratchet.Baseline(
        total_coverage_percent=_Unserialisable(),  # type: ignore[arg-type]
        diff_coverage_floor_percent=80,
    )
    with pytest.raises(TypeError):
        ratchet.write_baseline(baseline_path, bad)

    # Atomic replace means the original content is untouched.
    assert baseline_path.read_bytes() == original_bytes


# --------------------------------------------------------------------------- #
# compare / bump decision
# --------------------------------------------------------------------------- #


def test_decide_drop_fails_and_does_not_bump() -> None:
    decision = ratchet.decide(baseline_pct=17.5, measured_pct=16.0, tolerance=0.05)

    assert decision.dropped is True
    assert decision.should_bump is False
    assert decision.exit_code != 0


def test_decide_rise_passes_and_bumps() -> None:
    decision = ratchet.decide(baseline_pct=17.5, measured_pct=19.2, tolerance=0.05)

    assert decision.dropped is False
    assert decision.should_bump is True
    assert decision.new_total_pct == pytest.approx(19.2)
    assert decision.exit_code == 0


def test_decide_flat_within_tolerance_passes_without_bump() -> None:
    decision = ratchet.decide(baseline_pct=17.50, measured_pct=17.52, tolerance=0.05)

    assert decision.dropped is False
    assert decision.should_bump is False
    assert decision.exit_code == 0


def test_decide_tiny_drop_within_tolerance_does_not_fail() -> None:
    """Sub-tolerance noise (float jitter between runs) must not trip the gate."""
    decision = ratchet.decide(baseline_pct=17.50, measured_pct=17.48, tolerance=0.05)

    assert decision.dropped is False
    assert decision.exit_code == 0


def test_decide_drop_beyond_tolerance_fails() -> None:
    decision = ratchet.decide(baseline_pct=17.50, measured_pct=17.40, tolerance=0.05)

    assert decision.dropped is True
    assert decision.exit_code != 0


# --------------------------------------------------------------------------- #
# weekly diff-coverage floor increment + cap
# --------------------------------------------------------------------------- #


def test_weekly_bump_increments_by_step() -> None:
    assert ratchet.next_floor(current=80, step=1, cap=90) == 81


def test_weekly_bump_caps_at_ceiling() -> None:
    assert ratchet.next_floor(current=90, step=1, cap=90) == 90


def test_weekly_bump_does_not_overshoot_cap() -> None:
    assert ratchet.next_floor(current=89, step=5, cap=90) == 90


def test_weekly_bump_already_above_cap_clamps_down_to_cap() -> None:
    # Defensive: a manually-edited floor above the cap is clamped, never raised.
    assert ratchet.next_floor(current=95, step=1, cap=90) == 90


def test_weekly_bump_rejects_non_positive_step() -> None:
    with pytest.raises(ValueError):
        ratchet.next_floor(current=80, step=0, cap=90)


# --------------------------------------------------------------------------- #
# baseline provenance: re-deriving the committed percentage offline
# --------------------------------------------------------------------------- #

# The measurement that produced the 82.81 baseline committed by the ratchet:
# CI run 30886183592 on `main` at 11eb64d1. Kept here as a literal so the
# arithmetic that turns a Cobertura report into the committed number stays
# pinned to a real report rather than a hand-picked round figure.
_REFERENCE_LINE_RATE = "0.8281"
_REFERENCE_LINES_VALID = "233551"
_REFERENCE_LINES_COVERED = "193411"
_REFERENCE_PERCENT = 82.81
_REFERENCE_HEAD_SHA = "11eb64d1"
_REFERENCE_RUN_ID = "30886183592"


def _write_reference_coverage_xml(path: Path) -> None:
    """Write the Cobertura root of the real run 30886183592 report."""
    path.write_text(
        f'<?xml version="1.0" ?>\n'
        f'<coverage line-rate="{_REFERENCE_LINE_RATE}" branch-rate="0.6"'
        f' lines-valid="{_REFERENCE_LINES_VALID}"'
        f' lines-covered="{_REFERENCE_LINES_COVERED}" version="7.0">\n'
        f"  <packages></packages>\n"
        f"</coverage>\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize("bad", ["2.0", "-0.1", "1.5", "100"])
def test_parse_line_rate_rejects_out_of_range(tmp_path: Path, bad: str) -> None:
    """A line-rate outside [0, 1] is a broken report, not a coverage figure.

    ``2.0`` is the dangerous one: it derives to 200%, clears the baseline,
    and is written as the new high-water mark - after which every honest
    measurement reads as a catastrophic drop.
    """
    xml = tmp_path / "coverage.xml"
    _write_coverage_xml(xml, bad)

    with pytest.raises(ratchet.CoverageParseError, match="line-rate"):
        ratchet.parse_line_rate(xml)


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "Infinity"])
def test_parse_line_rate_rejects_non_finite(tmp_path: Path, bad: str) -> None:
    """NaN/inf survive float() and poison every comparison downstream."""
    xml = tmp_path / "coverage.xml"
    _write_coverage_xml(xml, bad)

    with pytest.raises(ratchet.CoverageParseError, match="line-rate"):
        ratchet.parse_line_rate(xml)


@pytest.mark.parametrize("edge", ["0", "0.0", "1", "1.0"])
def test_parse_line_rate_accepts_the_inclusive_bounds(tmp_path: Path, edge: str) -> None:
    """0% and 100% are legitimate reports and must still parse."""
    xml = tmp_path / "coverage.xml"
    _write_coverage_xml(xml, edge)

    assert ratchet.parse_line_rate(xml) == pytest.approx(float(edge), abs=1e-12)


def test_a_malformed_report_cannot_poison_the_baseline(tmp_path: Path) -> None:
    """End-to-end: `check` soft-skips on a 200% report and writes nothing."""
    baseline_path = tmp_path / ".coverage-baseline.json"
    ratchet.write_baseline(
        baseline_path,
        ratchet.Baseline(
            total_coverage_percent=_REFERENCE_PERCENT,
            diff_coverage_floor_percent=85,
            line_rate=0.8281,
        ),
    )
    before = baseline_path.read_bytes()
    xml = tmp_path / "coverage.xml"
    _write_coverage_xml(xml, "2.0")

    exit_code = ratchet.main(["check", "--coverage-xml", str(xml), "--baseline", str(baseline_path)])

    # Exit 3 = malformed report, the existing soft-skip contract.
    assert exit_code == 3
    assert baseline_path.read_bytes() == before, "a malformed report rewrote the baseline"


def test_init_refuses_a_malformed_report(tmp_path: Path) -> None:
    """`init` must not seed a baseline from a nonsense line-rate either."""
    xml = tmp_path / "coverage.xml"
    _write_coverage_xml(xml, "nan")
    baseline_path = tmp_path / ".coverage-baseline.json"

    assert ratchet.main(["init", "--coverage-xml", str(xml), "--baseline", str(baseline_path)]) == 3
    assert not baseline_path.exists()


def test_parse_line_rate_returns_the_raw_cobertura_fraction(tmp_path: Path) -> None:
    """The stored fraction is the report's own attribute, not a derived value."""
    xml = tmp_path / "coverage.xml"
    _write_reference_coverage_xml(xml)

    assert ratchet.parse_line_rate(xml) == pytest.approx(0.8281, abs=1e-12)


def test_reference_report_rounds_to_the_committed_baseline(tmp_path: Path) -> None:
    """Regression: run 30886183592's report must still yield 82.81%.

    Both routes to the percentage agree, so a stored line-rate is enough to
    re-derive the committed figure without the report:
      - root line-rate  0.8281              -> 82.81
      - lines-covered / lines-valid 82.8132 -> 82.81
    """
    xml = tmp_path / "coverage.xml"
    _write_reference_coverage_xml(xml)

    assert ratchet.parse_total_coverage(xml) == pytest.approx(_REFERENCE_PERCENT, abs=1e-9)
    assert ratchet.derive_percent_from_line_rate(ratchet.parse_line_rate(xml)) == pytest.approx(
        _REFERENCE_PERCENT, abs=1e-9
    )
    counter_percent = round(int(_REFERENCE_LINES_COVERED) / int(_REFERENCE_LINES_VALID) * 100.0, 2)
    assert counter_percent == pytest.approx(_REFERENCE_PERCENT, abs=1e-9)


def test_baseline_round_trips_provenance_fields(tmp_path: Path) -> None:
    baseline_path = tmp_path / ".coverage-baseline.json"
    ratchet.write_baseline(
        baseline_path,
        ratchet.Baseline(
            total_coverage_percent=_REFERENCE_PERCENT,
            diff_coverage_floor_percent=85,
            line_rate=0.8281,
            head_sha=_REFERENCE_HEAD_SHA,
            run_id=_REFERENCE_RUN_ID,
        ),
    )

    loaded = ratchet.read_baseline(baseline_path)

    assert loaded.line_rate == pytest.approx(0.8281, abs=1e-12)
    assert loaded.head_sha == _REFERENCE_HEAD_SHA
    assert loaded.run_id == _REFERENCE_RUN_ID


def test_write_baseline_omits_absent_provenance_keys(tmp_path: Path) -> None:
    """A baseline with no provenance writes no null keys."""
    baseline_path = tmp_path / ".coverage-baseline.json"
    ratchet.write_baseline(
        baseline_path,
        ratchet.Baseline(total_coverage_percent=20.0, diff_coverage_floor_percent=85),
    )

    data = json.loads(baseline_path.read_text(encoding="utf-8"))

    assert "line_rate" not in data
    assert "head_sha" not in data
    assert "run_id" not in data


def test_verify_consistency_accepts_a_baseline_that_re_derives(tmp_path: Path) -> None:
    baseline = ratchet.Baseline(
        total_coverage_percent=_REFERENCE_PERCENT,
        diff_coverage_floor_percent=85,
        line_rate=0.8281,
    )

    ratchet.verify_baseline_consistency(baseline)  # must not raise


def test_verify_consistency_rejects_a_hand_edited_percentage() -> None:
    """The stated percentage no longer follows from the stated line-rate."""
    baseline = ratchet.Baseline(
        total_coverage_percent=95.00,
        diff_coverage_floor_percent=85,
        line_rate=0.8281,
    )

    with pytest.raises(ratchet.BaselineConsistencyError, match="82.81"):
        ratchet.verify_baseline_consistency(baseline)


def test_verify_consistency_rejects_a_stale_line_rate() -> None:
    """The line-rate was left behind when the percentage moved."""
    baseline = ratchet.Baseline(
        total_coverage_percent=_REFERENCE_PERCENT,
        diff_coverage_floor_percent=85,
        line_rate=0.7751,
    )

    with pytest.raises(ratchet.BaselineConsistencyError):
        ratchet.verify_baseline_consistency(baseline)


def test_verify_consistency_tolerates_a_pre_provenance_baseline() -> None:
    """A baseline predating provenance has nothing to check; it must not raise."""
    baseline = ratchet.Baseline(total_coverage_percent=77.51, diff_coverage_floor_percent=85)

    ratchet.verify_baseline_consistency(baseline)  # must not raise


def test_check_refuses_a_baseline_whose_percentage_cannot_be_re_derived(tmp_path: Path) -> None:
    """A tampered baseline is a misconfiguration (exit 2), not a coverage verdict."""
    baseline_path = tmp_path / ".coverage-baseline.json"
    baseline_path.write_text(
        json.dumps(
            {
                "diff_coverage_floor_percent": 85,
                "line_rate": 0.8281,
                "total_coverage_percent": 50.00,
            }
        ),
        encoding="utf-8",
    )
    xml = tmp_path / "coverage.xml"
    _write_reference_coverage_xml(xml)

    exit_code = ratchet.main(["check", "--coverage-xml", str(xml), "--baseline", str(baseline_path)])

    assert exit_code == 2
    # The tampered baseline is left exactly as found - no silent repair.
    assert json.loads(baseline_path.read_text(encoding="utf-8"))["total_coverage_percent"] == 50.00


def test_check_records_provenance_when_the_ratchet_clicks(tmp_path: Path) -> None:
    baseline_path = tmp_path / ".coverage-baseline.json"
    ratchet.write_baseline(
        baseline_path,
        ratchet.Baseline(
            total_coverage_percent=70.00,
            diff_coverage_floor_percent=85,
            line_rate=0.70,
        ),
    )
    xml = tmp_path / "coverage.xml"
    _write_reference_coverage_xml(xml)

    exit_code = ratchet.main(
        [
            "check",
            "--coverage-xml",
            str(xml),
            "--baseline",
            str(baseline_path),
            "--head-sha",
            _REFERENCE_HEAD_SHA,
            "--run-id",
            _REFERENCE_RUN_ID,
        ]
    )

    assert exit_code == 0
    bumped = ratchet.read_baseline(baseline_path)
    assert bumped.total_coverage_percent == pytest.approx(_REFERENCE_PERCENT, abs=1e-9)
    assert bumped.line_rate == pytest.approx(0.8281, abs=1e-12)
    assert bumped.head_sha == _REFERENCE_HEAD_SHA
    assert bumped.run_id == _REFERENCE_RUN_ID
    # The bump it just wrote must itself re-derive.
    ratchet.verify_baseline_consistency(bumped)


def test_bump_floor_preserves_total_coverage_provenance(tmp_path: Path) -> None:
    """The weekly floor bump moves LEVEL 1 only; LEVEL 2's provenance survives."""
    baseline_path = tmp_path / ".coverage-baseline.json"
    ratchet.write_baseline(
        baseline_path,
        ratchet.Baseline(
            total_coverage_percent=_REFERENCE_PERCENT,
            diff_coverage_floor_percent=85,
            line_rate=0.8281,
            head_sha=_REFERENCE_HEAD_SHA,
            run_id=_REFERENCE_RUN_ID,
        ),
    )

    assert ratchet.main(["bump-floor", "--baseline", str(baseline_path)]) == 0

    after = ratchet.read_baseline(baseline_path)
    assert after.diff_coverage_floor_percent == 86
    assert after.line_rate == pytest.approx(0.8281, abs=1e-12)
    assert after.head_sha == _REFERENCE_HEAD_SHA
    assert after.run_id == _REFERENCE_RUN_ID


def test_verify_command_re_derives_offline(tmp_path: Path) -> None:
    """`verify` needs only the committed file - no coverage.xml, no network."""
    baseline_path = tmp_path / ".coverage-baseline.json"
    ratchet.write_baseline(
        baseline_path,
        ratchet.Baseline(
            total_coverage_percent=_REFERENCE_PERCENT,
            diff_coverage_floor_percent=85,
            line_rate=0.8281,
            head_sha=_REFERENCE_HEAD_SHA,
            run_id=_REFERENCE_RUN_ID,
        ),
    )

    assert ratchet.main(["verify", "--baseline", str(baseline_path)]) == 0


def test_verify_command_fails_on_an_inconsistent_baseline(tmp_path: Path) -> None:
    baseline_path = tmp_path / ".coverage-baseline.json"
    baseline_path.write_text(
        json.dumps(
            {
                "diff_coverage_floor_percent": 85,
                "line_rate": 0.8281,
                "total_coverage_percent": 90.00,
            }
        ),
        encoding="utf-8",
    )

    assert ratchet.main(["verify", "--baseline", str(baseline_path)]) == 2


def test_verify_command_requires_provenance_when_asked(tmp_path: Path) -> None:
    """`--require-provenance` is how CI asserts the committed file is checkable."""
    baseline_path = tmp_path / ".coverage-baseline.json"
    ratchet.write_baseline(
        baseline_path,
        ratchet.Baseline(total_coverage_percent=77.51, diff_coverage_floor_percent=85),
    )

    assert ratchet.main(["verify", "--baseline", str(baseline_path)]) == 0
    assert ratchet.main(["verify", "--baseline", str(baseline_path), "--require-provenance"]) == 2


# --------------------------------------------------------------------------- #
# the baseline this repo actually commits
# --------------------------------------------------------------------------- #


def test_committed_baseline_carries_provenance_and_re_derives() -> None:
    """The number in the repo must be reproducible from the repo alone.

    This is the loud failure for a hand-edited or half-updated baseline:
    the committed percentage has to follow from the committed line-rate,
    and the measurement has to name the commit it came from.
    """
    baseline_path = Path(__file__).resolve().parents[2] / ".coverage-baseline.json"
    baseline = ratchet.read_baseline(baseline_path)

    assert baseline.line_rate is not None, "committed baseline has no line_rate to verify against"
    assert baseline.head_sha, "committed baseline does not name the commit it was measured on"
    ratchet.verify_baseline_consistency(baseline)


# --------------------------------------------------------------------------- #
# the ratchet-PR guard: is the open PR still pushable?
# --------------------------------------------------------------------------- #
#
# The guard step in ``.github/workflows/coverage-ratchet.yml`` decides
# whether this fire may update the single open ratchet PR. It refuses for
# two unrelated reasons and they must stay distinguishable in the log:
#
#   * the measurement is not above the mark the open PR already carries -
#     force-pushing it would move the high-water mark DOWN;
#   * the open PR is sitting in the merge queue, which locks its branch
#     against pushes, so the push cannot land whatever the number says.
#
# The second one used to be absent, and the create-pull-request step then
# hard-failed on ``GH006 ... cannot be updated`` for the whole time a
# ratchet PR spent in the queue - a red mark on main that meant nothing
# was wrong.


RATCHET_BRANCH = "coverage-ratchet/baseline"


def _write_base_baseline(tmp_path: Path, pct: float) -> Path:
    """The baseline committed on the branch the ratchet PR is opened onto."""
    path = tmp_path / "base-baseline.json"
    path.write_text(json.dumps({"total_coverage_percent": pct}), encoding="utf-8")
    return path


def test_a_queued_ratchet_pr_refuses_because_its_branch_is_locked() -> None:
    """A queued PR's branch rejects pushes, so a higher number changes nothing."""
    verdict = ratchet.guard_decision(
        measured_pct=91.0,
        open_pct=83.7,
        queued_branches=[RATCHET_BRANCH],
        ratchet_branch=RATCHET_BRANCH,
    )

    assert verdict.proceed is False
    assert "merge queue" in verdict.notice


def test_the_queued_refusal_is_distinguishable_from_the_downward_refusal() -> None:
    """Two skip paths, two reasons: a shared message hides which one fired."""
    queued = ratchet.guard_decision(
        measured_pct=91.0,
        open_pct=83.7,
        queued_branches=[RATCHET_BRANCH],
        ratchet_branch=RATCHET_BRANCH,
    )
    downward = ratchet.guard_decision(
        measured_pct=83.0,
        open_pct=83.7,
        queued_branches=[],
        ratchet_branch=RATCHET_BRANCH,
    )

    assert queued.proceed is downward.proceed is False
    assert queued.notice != downward.notice
    assert queued.notice not in downward.notice
    assert downward.notice not in queued.notice


def test_an_unrelated_pr_in_the_queue_does_not_block_the_ratchet() -> None:
    """Only the ratchet's own branch being queued locks the ratchet's branch."""
    verdict = ratchet.guard_decision(
        measured_pct=91.0,
        open_pct=83.7,
        queued_branches=["fix/3928-something-else", "feat/unrelated"],
        ratchet_branch=RATCHET_BRANCH,
    )

    assert verdict.proceed is True


def test_an_open_but_unqueued_ratchet_pr_is_still_updated_in_place() -> None:
    """The pre-existing behaviour: a higher mark rewrites the open PR."""
    verdict = ratchet.guard_decision(
        measured_pct=83.71,
        open_pct=83.7,
        queued_branches=[],
        ratchet_branch=RATCHET_BRANCH,
    )

    assert verdict.proceed is True
    assert "updating it" in verdict.notice


def test_a_measurement_below_the_open_mark_still_refuses() -> None:
    """The downward guard is what stops the ratchet from running backwards."""
    verdict = ratchet.guard_decision(
        measured_pct=83.69,
        open_pct=83.7,
        queued_branches=[],
        ratchet_branch=RATCHET_BRANCH,
    )

    assert verdict.proceed is False
    assert "not above" in verdict.notice


def test_an_absent_ratchet_branch_opens_a_fresh_pr() -> None:
    """No open ratchet PR: nothing to guard against, nothing to be queued."""
    verdict = ratchet.guard_decision(
        measured_pct=83.71,
        open_pct=None,
        queued_branches=[],
        ratchet_branch=RATCHET_BRANCH,
    )

    assert verdict.proceed is True
    assert "fresh" in verdict.notice


def test_the_queue_lock_outranks_the_downward_reason() -> None:
    """Both refusals apply at once; report the one that makes the push impossible.

    The number can be argued about on the next fire. The branch lock cannot:
    while the PR is queued no push lands, so that is the fact worth logging.
    Pinning the precedence keeps the message deterministic rather than an
    artifact of which condition happens to be evaluated first.
    """
    verdict = ratchet.guard_decision(
        measured_pct=83.0,
        open_pct=83.7,
        queued_branches=[RATCHET_BRANCH],
        ratchet_branch=RATCHET_BRANCH,
    )

    assert verdict.proceed is False
    assert "merge queue" in verdict.notice


def test_guard_command_writes_proceed_false_to_github_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end through the subcommand the workflow actually calls."""
    open_baseline = tmp_path / "open-baseline.json"
    open_baseline.write_text(json.dumps({"total_coverage_percent": 83.7}), encoding="utf-8")
    base_baseline = _write_base_baseline(tmp_path, 83.0)
    queued = tmp_path / "queued.json"
    queued.write_text(json.dumps([RATCHET_BRANCH]), encoding="utf-8")
    gh_output = tmp_path / "gh-output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(gh_output))

    rc = ratchet.main(
        [
            "guard",
            "--measured",
            "91.0",
            "--open-baseline",
            str(open_baseline),
            "--base-baseline",
            str(base_baseline),
            "--queued-branches",
            str(queued),
            "--ratchet-branch",
            RATCHET_BRANCH,
        ]
    )

    assert rc == 0, "a refusal is a normal outcome, not a job failure"
    assert "proceed=false" in gh_output.read_text(encoding="utf-8")


def test_guard_command_treats_a_missing_open_baseline_as_an_absent_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The workflow writes that file only when the branch actually has one."""
    base_baseline = _write_base_baseline(tmp_path, 83.0)
    queued = tmp_path / "queued.json"
    queued.write_text("[]", encoding="utf-8")
    gh_output = tmp_path / "gh-output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(gh_output))

    rc = ratchet.main(
        [
            "guard",
            "--measured",
            "83.71",
            "--open-baseline",
            str(tmp_path / "does-not-exist.json"),
            "--base-baseline",
            str(base_baseline),
            "--queued-branches",
            str(queued),
            "--ratchet-branch",
            RATCHET_BRANCH,
        ]
    )

    assert rc == 0
    assert "proceed=true" in gh_output.read_text(encoding="utf-8")


def test_an_unreadable_queue_file_refuses_instead_of_assuming_an_empty_queue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parsing the queue as empty on error would re-open the bug it closes.

    An empty queue means "the branch is pushable". Reaching that conclusion
    from a failed read is how the guard would go back to hard-failing on
    ``GH006`` while reporting that everything was fine.
    """
    open_baseline = tmp_path / "open-baseline.json"
    open_baseline.write_text(json.dumps({"total_coverage_percent": 83.7}), encoding="utf-8")
    base_baseline = _write_base_baseline(tmp_path, 83.0)
    queued = tmp_path / "queued.json"
    queued.write_text("{not json", encoding="utf-8")
    gh_output = tmp_path / "gh-output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(gh_output))

    rc = ratchet.main(
        [
            "guard",
            "--measured",
            "91.0",
            "--open-baseline",
            str(open_baseline),
            "--base-baseline",
            str(base_baseline),
            "--queued-branches",
            str(queued),
            "--ratchet-branch",
            RATCHET_BRANCH,
        ]
    )

    assert rc != 0, "an unreadable queue must be loud, not silently empty"
    assert "proceed=true" not in gh_output.read_text(encoding="utf-8") if gh_output.exists() else True


# --------------------------------------------------------------------------- #
# the stale-read refusal: is there anything left to raise on the base branch?
# --------------------------------------------------------------------------- #
#
# `check` reads the baseline out of the tree it checked out, and that tree is
# the measured commit rather than main - deliberately, so the coverage report
# and the tree it is attributed to are the same commit. By the time a commit's
# CI run completes, main has often already ratcheted past the mark that commit
# carries. `check` then re-reads a superseded mark as if it were still pending
# and bumps to a value main already holds: correct against what it read, a
# no-op against the branch the PR is opened onto.
#
# The visible artefact is a pull request whose diff moves head_sha, run_id and
# updated_at while line_rate and total_coverage_percent stay byte-identical -
# provenance travelling without a measurement, which is what #4087 named.


def test_a_bump_to_a_mark_the_base_already_carries_is_refused() -> None:
    """The whole of #4087: equal marks mean the PR would carry only provenance."""
    verdict = ratchet.guard_decision(
        measured_pct=83.84,
        open_pct=None,
        queued_branches=[],
        ratchet_branch=RATCHET_BRANCH,
        base_pct=83.84,
    )

    assert verdict.proceed is False
    assert "provenance" in verdict.notice


def test_issue_4087_is_refused_with_the_numbers_that_produced_it() -> None:
    """PR #4085's actual arithmetic, so the regression is pinned to the artefact.

    Commit ``6bb9f3ed`` carries 83.77 in its tree. Its CI run completed at
    08:43, by which point #4082 had already ratcheted main to 83.84. `check`
    measured 83.84 against the 83.77 it read, cleared the 0.05 tolerance by
    0.07 and bumped - onto a base that was already at 83.84. The resulting
    diff moved head_sha and run_id and nothing else.
    """
    measured_on_a_stale_tree = ratchet.decide(baseline_pct=83.77, measured_pct=83.84)
    assert measured_on_a_stale_tree.should_bump is True, (
        "the write path is not the defect; it bumps correctly against what it read"
    )

    verdict = ratchet.guard_decision(
        measured_pct=83.84,
        open_pct=None,
        queued_branches=[],
        ratchet_branch=RATCHET_BRANCH,
        base_pct=83.84,
    )

    assert verdict.proceed is False, "#4085 must not open"


def test_a_real_rise_above_the_base_still_opens_its_pr() -> None:
    """The guard must not cost the ratchet its actual job."""
    verdict = ratchet.guard_decision(
        measured_pct=83.90,
        open_pct=None,
        queued_branches=[],
        ratchet_branch=RATCHET_BRANCH,
        base_pct=83.84,
    )

    assert verdict.proceed is True


def test_the_stale_read_refusal_is_distinguishable_from_the_downward_one() -> None:
    """Three refusals, three reasons; a shared message hides which one fired."""
    stale = ratchet.guard_decision(
        measured_pct=83.84,
        open_pct=83.90,
        queued_branches=[],
        ratchet_branch=RATCHET_BRANCH,
        base_pct=83.84,
    )
    downward = ratchet.guard_decision(
        measured_pct=83.86,
        open_pct=83.90,
        queued_branches=[],
        ratchet_branch=RATCHET_BRANCH,
        base_pct=83.84,
    )
    queued = ratchet.guard_decision(
        measured_pct=83.95,
        open_pct=83.90,
        queued_branches=[RATCHET_BRANCH],
        ratchet_branch=RATCHET_BRANCH,
        base_pct=83.84,
    )

    assert stale.proceed is downward.proceed is queued.proceed is False
    assert len({stale.notice, downward.notice, queued.notice}) == 3


def test_a_measurement_between_the_base_and_the_open_pr_still_refuses_downward() -> None:
    """Clearing the base does not license rewriting the open PR downward.

    The open ratchet PR's mark sits above main's for as long as it is open,
    so this is the band where the two refusals do genuinely different work.
    """
    verdict = ratchet.guard_decision(
        measured_pct=83.86,
        open_pct=83.90,
        queued_branches=[],
        ratchet_branch=RATCHET_BRANCH,
        base_pct=83.84,
    )

    assert verdict.proceed is False
    assert "not above the open ratchet PR" in verdict.notice


def test_the_queue_lock_outranks_the_stale_read_reason() -> None:
    """Deterministic precedence: the impossible push is the fact worth logging."""
    verdict = ratchet.guard_decision(
        measured_pct=83.84,
        open_pct=83.90,
        queued_branches=[RATCHET_BRANCH],
        ratchet_branch=RATCHET_BRANCH,
        base_pct=83.84,
    )

    assert verdict.proceed is False
    assert "merge queue" in verdict.notice


def test_the_stale_read_refusal_applies_with_no_ratchet_branch_open() -> None:
    """A provenance-only diff opens a FRESH PR just as readily as it rewrites one.

    Checking the base only when a ratchet branch happens to exist would leave
    #4087 reachable on every fire that follows a merged ratchet PR - which is
    exactly the state #4085 was opened in.
    """
    verdict = ratchet.guard_decision(
        measured_pct=83.84,
        open_pct=None,
        queued_branches=[],
        ratchet_branch=RATCHET_BRANCH,
        base_pct=83.84,
    )

    assert verdict.proceed is False
    assert "fresh" not in verdict.notice


def test_bump_floor_is_untouched_by_the_provenance_rule(tmp_path: Path) -> None:
    """Why the rule lives in the guard and NOT in write_baseline.

    The obvious formulation - refuse a payload whose measurement fields are
    unchanged from the file on disk - would refuse the weekly floor bump,
    whose entire job is to move `diff_coverage_floor_percent` and nothing
    else. `write_baseline` stays honest; the guard is where the base branch
    is actually known.
    """
    baseline_path = tmp_path / ".coverage-baseline.json"
    ratchet.write_baseline(
        baseline_path,
        ratchet.Baseline(
            total_coverage_percent=83.84,
            diff_coverage_floor_percent=86,
            updated_at="2026-08-17T08:04:51+00:00",
            line_rate=0.8384,
            head_sha="6902f699a30d1193a09a99129114af1b08920dfc",
            run_id="31999416744",
        ),
    )

    rc = ratchet.main(["bump-floor", "--baseline", str(baseline_path), "--step", "1", "--cap", "90"])

    after = ratchet.read_baseline(baseline_path)
    assert rc == 0
    assert after.diff_coverage_floor_percent == 87
    assert after.total_coverage_percent == 83.84
    assert after.line_rate == 0.8384


def test_guard_command_refuses_loudly_when_the_base_baseline_is_unreadable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The base branch always has a baseline, so a failed read is a broken read.

    Treating it as absent would silently retire the refusal while the job
    stayed green - the same failure shape the merge-queue read is guarded
    against.
    """
    queued = tmp_path / "queued.json"
    queued.write_text("[]", encoding="utf-8")
    gh_output = tmp_path / "gh-output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(gh_output))

    rc = ratchet.main(
        [
            "guard",
            "--measured",
            "83.84",
            "--open-baseline",
            str(tmp_path / "does-not-exist.json"),
            "--base-baseline",
            str(tmp_path / "also-does-not-exist.json"),
            "--queued-branches",
            str(queued),
            "--ratchet-branch",
            RATCHET_BRANCH,
        ]
    )

    assert rc != 0
    assert "proceed=true" not in (gh_output.read_text(encoding="utf-8") if gh_output.exists() else "")


def test_guard_command_cannot_be_invoked_without_a_base_baseline() -> None:
    """Optional would mean one edited workflow line silently disables the guard."""
    with pytest.raises(SystemExit):
        ratchet.main(
            [
                "guard",
                "--measured",
                "83.84",
                "--open-baseline",
                "open.json",
                "--queued-branches",
                "queued.json",
                "--ratchet-branch",
                RATCHET_BRANCH,
            ]
        )
