"""Issue #5105: how many capabilities have more than one implementation, as a standing number.

A guard test answers "did THIS PR introduce a second implementation". It cannot
answer "how many are there now, and did that number move since last week" --
that needs something that runs standalone and reports state rather than gating a
diff.

`core/security/security_posture.py` is the proof this codebase has tried once
already: 347 lines that compute an A-F letter from weighted metrics, with zero
callers anywhere. Its grading model is explicitly rejected here.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from bernstein.core.govern.duplication_audit import (
    DuplicationFinding,
    Verdict,
    collect_duplication,
)

if TYPE_CHECKING:
    from pathlib import Path

SRC = "src/bernstein"


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "pkg"
    for name, body in files.items():
        _write(root / name, body)
    return root


CANONICAL = (
    "import json\n\n\n"
    "def encode(payload: dict) -> bytes:\n"
    '    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")\n'
)


# ---------------------------------------------------------------------------
# The load-bearing property
# ---------------------------------------------------------------------------


def test_finding_byte_identical_across_two_runs(tmp_path: Path) -> None:
    """Determinism: same tree, same findings, same bytes.

    A report an operator compares week to week is worthless if two runs over one
    tree disagree, and path ordering is the usual way that happens.
    """
    root = _tree(tmp_path, {"a.py": CANONICAL, "b.py": CANONICAL, "c.py": "x = 1\n"})

    first = json.dumps(collect_duplication(root).to_dict(), sort_keys=True)
    second = json.dumps(collect_duplication(root).to_dict(), sort_keys=True)

    assert first == second


def test_duplication_count_above_expected_produces_a_measured_failed_finding(tmp_path: Path) -> None:
    """The core report-shape contract."""
    root = _tree(tmp_path, {"a.py": CANONICAL, "b.py": CANONICAL})

    finding = _find(collect_duplication(root).findings, "inline-canonical-bytes-sites")

    assert finding.verdict is Verdict.MEASURED_FAILED
    assert finding.count == 2
    assert finding.expected == 1
    # A finding names WHERE, or the reader repeats the search the collector did.
    assert len(finding.paths) == 2
    assert all(":" in path for path in finding.paths)


def test_collector_reports_not_yet_measurable_for_unimplemented_checks(tmp_path: Path) -> None:
    """Honesty when a sibling issue has not landed -- distinct from "measured, passed".

    Reporting an unbuilt check as a pass is what makes an aggregate look like
    coverage it does not have.
    """
    report = collect_duplication(_tree(tmp_path, {"a.py": "x = 1\n"}))

    pending = {f.check_id for f in report.not_yet_measurable}
    assert "receipt-verify-kinds" in pending
    assert "registry-duplicate-ids" in pending
    for finding in report.not_yet_measurable:
        assert finding.verdict is Verdict.NOT_YET_MEASURABLE
        assert finding.count is None, "nothing was counted, so no count may be reported"


def test_no_score_or_grade_in_output(tmp_path: Path) -> None:
    """Guards against reintroducing security_posture.py's rejected A-F model."""
    document = collect_duplication(_tree(tmp_path, {"a.py": CANONICAL})).to_dict()
    serialized = json.dumps(document)

    for banned in ("score", "grade", "percent", "rating"):
        assert banned not in serialized, f"the report must not carry a {banned}"
    # And no bare letter verdict anywhere.
    for finding in document["findings"]:
        assert finding["verdict"] in {v.value for v in Verdict}


# ---------------------------------------------------------------------------
# What each check counts
# ---------------------------------------------------------------------------


def test_one_shared_encoder_passes(tmp_path: Path) -> None:
    report = collect_duplication(_tree(tmp_path, {"a.py": CANONICAL}))
    finding = _find(report.findings, "inline-canonical-bytes-sites")

    assert finding.verdict is Verdict.MEASURED_PASSED
    assert finding.count == 1
    # A passing check lists nothing: its one legitimate site is noise beside the failures.
    assert finding.paths == ()


def test_ordinary_sorted_json_is_not_counted(tmp_path: Path) -> None:
    """Sorted keys alone is how a great deal of logging and diffing serializes."""
    pretty = "import json\n\n\ndef log(x: dict) -> str:\n    return json.dumps(x, sort_keys=True, indent=2)\n"
    report = collect_duplication(_tree(tmp_path, {"a.py": pretty}))

    assert _find(report.findings, "inline-canonical-bytes-sites").count == 0


def test_private_hmac_loaders_are_counted_and_expected_to_be_zero(tmp_path: Path) -> None:
    loader = (
        "def _load_hmac_key() -> bytes:\n"
        "    from bernstein.core.security.audit import load_or_create_audit_key\n\n"
        "    return load_or_create_audit_key()\n"
    )
    report = collect_duplication(_tree(tmp_path, {"a.py": loader, "b.py": loader}))
    finding = _find(report.findings, "private-hmac-key-loaders")

    assert finding.expected == 0
    assert finding.count == 2
    assert finding.verdict is Verdict.MEASURED_FAILED


def test_a_tree_with_no_private_loaders_passes(tmp_path: Path) -> None:
    report = collect_duplication(_tree(tmp_path, {"a.py": "x = 1\n"}))
    assert _find(report.findings, "private-hmac-key-loaders").verdict is Verdict.MEASURED_PASSED


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


def test_findings_are_ordered_by_check_id(tmp_path: Path) -> None:
    ids = [f.check_id for f in collect_duplication(_tree(tmp_path, {"a.py": "x = 1\n"})).findings]
    assert ids == sorted(ids)


def test_the_summary_never_folds_unmeasured_checks_into_the_total(tmp_path: Path) -> None:
    """A denominator that silently includes what was not measured is the failure mode."""
    document = collect_duplication(_tree(tmp_path, {"a.py": CANONICAL})).to_dict()

    assert document["measured"] + document["not_yet_measurable"] == len(document["findings"])
    assert document["not_yet_measurable"] > 0


def test_an_unparseable_file_does_not_take_the_report_down(tmp_path: Path) -> None:
    """This is a report ABOUT the tree; one bad file must not stop it."""
    root = _tree(tmp_path, {"good.py": CANONICAL, "bad.py": "def broken( :\n"})

    assert _find(collect_duplication(root).findings, "inline-canonical-bytes-sites").count == 1


def test_it_runs_against_this_repository(tmp_path: Path) -> None:
    """The collector's actual job. Asserts the SHAPE, never a live count.

    Pinning today's number here would make the test fail the moment somebody
    fixes the duplication it exists to report.
    """
    from pathlib import Path as RealPath

    root = RealPath(__file__).resolve().parents[3] / SRC
    report = collect_duplication(root)

    assert len(report.findings) == 6
    measured = [f for f in report.findings if f.verdict is not Verdict.NOT_YET_MEASURABLE]
    assert len(measured) == 2
    for finding in measured:
        assert finding.count is not None
        if finding.verdict is Verdict.MEASURED_FAILED:
            assert finding.count > finding.expected
            assert finding.paths, "a failing check must name where"


def _find(findings: tuple[DuplicationFinding, ...], check_id: str) -> DuplicationFinding:
    for finding in findings:
        if finding.check_id == check_id:
            return finding
    raise AssertionError(f"no finding {check_id!r}")
