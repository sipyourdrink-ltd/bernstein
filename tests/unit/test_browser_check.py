"""Browser flow report: boundary refusals and the offline verdict resolver (#2523).

A browser flow report is not a screenshot folder with a pass/fail note next to
it. It is the Merkle-chained action journal itself: every step pins the exact
observation bytes the worker saw, every anchor folds in its predecessor, and
every check verdict is *recomputed* from the reattached bytes rather than trusted
as recorded. These tests pin both halves of that contract:

* :func:`validate_browser_flow_report` refuses a malformed or broken-chain report
  before it can be dispatched; and
* :func:`verify_browser_flow_report` resolves the whole report offline against a
  content store, naming the exact failing step index or check id.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from bernstein.core.agents.computer_use import GENESIS_ANCHOR, Action, ActionKind
from bernstein.core.orchestration.activity import ActivityRejected
from bernstein.core.orchestration.activity_modalities import ContentStore
from bernstein.core.orchestration.browser_check import (
    BrowserCheckRecord,
    BrowserFlowReport,
    BrowserStepRecord,
    CheckKind,
    build_step_record,
    evaluate_check,
    normalise_dom,
    report_to_canonical_bytes,
    validate_browser_flow_report,
    verify_browser_flow_report,
)

_FRAMES: tuple[tuple[bytes, bytes], ...] = (
    (b"png-0", b"<html>  Sign   in\n</html>"),
    (b"png-1", b"<html>Welcome back</html>"),
)


def _report(store: ContentStore) -> BrowserFlowReport:
    """Build a two-step report whose bytes are stored, mirroring a worker run."""
    steps: list[BrowserStepRecord] = []
    prev = GENESIS_ANCHOR
    actions = (Action(kind=ActionKind.CLICK, target="#login"), Action(kind=ActionKind.SCREENSHOT))
    for index, ((screenshot, dom), action) in enumerate(zip(_FRAMES, actions, strict=True)):
        record = build_step_record(
            index=index,
            prev_anchor=prev,
            action=action,
            screenshot_content_hash=store.put(screenshot),
            dom_content_hash=store.put(dom),
            screenshot_bytes=screenshot,
            dom_bytes=dom,
        )
        steps.append(record)
        prev = record.anchor
    checks = (
        BrowserCheckRecord(check_id="k1", kind=CheckKind.DOM_CONTAINS, operand="Sign in", step_index=0, passed=True),
        BrowserCheckRecord(
            check_id="k2", kind=CheckKind.DOM_CONTAINS, operand="Welcome back", step_index=1, passed=True
        ),
    )
    return BrowserFlowReport(
        flow_id="login-flow",
        start_url="https://shop/",
        steps=tuple(steps),
        checks=checks,
        head_anchor=prev,
    )


# ---------------------------------------------------------------------------
# deterministic DOM normalisation
# ---------------------------------------------------------------------------


def test_normalise_dom_collapses_cosmetic_whitespace() -> None:
    assert normalise_dom(b"<html>  Sign   in\n</html>") == normalise_dom(b"<html> Sign in </html>")


def test_normalise_dom_preserves_content_differences() -> None:
    assert normalise_dom(b"<html>a</html>") != normalise_dom(b"<html>b</html>")


def test_dom_contains_matches_across_cosmetic_whitespace() -> None:
    assert evaluate_check(
        kind=CheckKind.DOM_CONTAINS,
        operand="Sign in",
        dom_bytes=b"<html>  Sign   in\n</html>",
        screenshot_content_hash="sha256:deadbeef",
    )


def test_dom_not_contains_is_the_exact_inverse() -> None:
    kwargs = {"dom_bytes": b"<html>Error 500</html>", "screenshot_content_hash": "sha256:x"}
    assert evaluate_check(kind=CheckKind.DOM_NOT_CONTAINS, operand="Welcome", **kwargs)  # type: ignore[arg-type]
    assert not evaluate_check(kind=CheckKind.DOM_NOT_CONTAINS, operand="Error 500", **kwargs)  # type: ignore[arg-type]


def test_screenshot_hash_check_compares_the_pinned_hash() -> None:
    assert evaluate_check(
        kind=CheckKind.SCREENSHOT_HASH_EQUALS,
        operand="sha256:abc",
        dom_bytes=b"",
        screenshot_content_hash="sha256:abc",
    )
    assert not evaluate_check(
        kind=CheckKind.SCREENSHOT_HASH_EQUALS,
        operand="sha256:abc",
        dom_bytes=b"",
        screenshot_content_hash="sha256:def",
    )


# ---------------------------------------------------------------------------
# boundary refusals
# ---------------------------------------------------------------------------


def test_valid_report_passes_the_boundary(tmp_path: Path) -> None:
    store = ContentStore(tmp_path / "cas")
    assert validate_browser_flow_report(_report(store)) is not None


def test_report_with_empty_flow_id_is_refused(tmp_path: Path) -> None:
    store = ContentStore(tmp_path / "cas")
    report = _report(store)
    with pytest.raises(ActivityRejected, match="flow_id"):
        validate_browser_flow_report(replace(report, flow_id="  "))


def test_report_with_non_contiguous_step_indices_is_refused(tmp_path: Path) -> None:
    store = ContentStore(tmp_path / "cas")
    report = _report(store)
    broken = replace(report.steps[0], index=7)
    with pytest.raises(ActivityRejected, match="step index"):
        validate_browser_flow_report(replace(report, steps=(broken, report.steps[1])))


def test_report_with_broken_anchor_linkage_is_refused(tmp_path: Path) -> None:
    store = ContentStore(tmp_path / "cas")
    report = _report(store)
    broken = replace(report.steps[1], prev_anchor="0" * 64)
    with pytest.raises(ActivityRejected, match="prev_anchor"):
        validate_browser_flow_report(replace(report, steps=(report.steps[0], broken)))


def test_report_whose_head_anchor_is_not_the_last_anchor_is_refused(tmp_path: Path) -> None:
    store = ContentStore(tmp_path / "cas")
    report = _report(store)
    with pytest.raises(ActivityRejected, match="head_anchor"):
        validate_browser_flow_report(replace(report, head_anchor="f" * 64))


def test_report_with_duplicate_check_ids_is_refused(tmp_path: Path) -> None:
    store = ContentStore(tmp_path / "cas")
    report = _report(store)
    dupe = replace(report.checks[1], check_id="k1")
    with pytest.raises(ActivityRejected, match="duplicate check_id"):
        validate_browser_flow_report(replace(report, checks=(report.checks[0], dupe)))


def test_check_pointing_past_the_last_step_is_refused(tmp_path: Path) -> None:
    store = ContentStore(tmp_path / "cas")
    report = _report(store)
    stray = replace(report.checks[0], step_index=99)
    with pytest.raises(ActivityRejected, match="step_index"):
        validate_browser_flow_report(replace(report, checks=(stray,)))


def test_check_with_an_empty_operand_is_refused(tmp_path: Path) -> None:
    store = ContentStore(tmp_path / "cas")
    report = _report(store)
    empty = replace(report.checks[0], operand="")
    with pytest.raises(ActivityRejected, match="operand"):
        validate_browser_flow_report(replace(report, checks=(empty,)))


def test_step_with_a_malformed_content_hash_is_refused(tmp_path: Path) -> None:
    store = ContentStore(tmp_path / "cas")
    report = _report(store)
    bad = replace(report.steps[0], screenshot_content_hash="not-a-hash")
    with pytest.raises(ActivityRejected, match="content hash"):
        validate_browser_flow_report(replace(report, steps=(bad, report.steps[1])))


def test_empty_flow_must_carry_the_genesis_head(tmp_path: Path) -> None:
    report = BrowserFlowReport(flow_id="f", start_url="https://x/", steps=(), checks=(), head_anchor=GENESIS_ANCHOR)
    assert validate_browser_flow_report(report) is report
    with pytest.raises(ActivityRejected, match="head_anchor"):
        validate_browser_flow_report(replace(report, head_anchor="a" * 64))


# ---------------------------------------------------------------------------
# offline verification
# ---------------------------------------------------------------------------


def test_report_verifies_offline_against_the_store(tmp_path: Path) -> None:
    store = ContentStore(tmp_path / "cas")
    verdict = verify_browser_flow_report(_report(store), store=store)
    assert verdict.ok
    assert [s.index for s in verdict.steps] == [0, 1]
    assert all(s.ok for s in verdict.steps)
    assert [c.check_id for c in verdict.checks] == ["k1", "k2"]
    assert verdict.head_anchor_ok


def test_missing_evidence_bytes_fail_naming_the_step(tmp_path: Path) -> None:
    store = ContentStore(tmp_path / "cas")
    report = _report(store)
    (tmp_path / "cas" / report.steps[1].dom_content_hash.split(":", 1)[1]).unlink()
    verdict = verify_browser_flow_report(report, store=store)
    assert not verdict.ok
    failed = next(s for s in verdict.steps if not s.ok)
    assert failed.index == 1
    assert "missing" in failed.reason


def test_round_trip_through_the_json_projection_is_lossless(tmp_path: Path) -> None:
    store = ContentStore(tmp_path / "cas")
    report = _report(store)
    rebuilt = BrowserFlowReport.from_dict(report.to_dict())
    assert rebuilt == report
    assert report_to_canonical_bytes(rebuilt) == report_to_canonical_bytes(report)
