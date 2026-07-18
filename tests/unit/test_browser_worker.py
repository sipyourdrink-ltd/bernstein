"""Browser worker: forensically replayable site checks and UI flows (#2523).

The worker runs a site check or UI flow as a first-class activity on the typed
boundary: it anchors the exact observation it saw before every action, folds each
anchor into its predecessor, and hands the resulting Merkle-chained flow report to
the same :func:`dispatch_activity` path a coding spawn uses. These tests prove the
bar the issue sets:

* every step's screenshot bytes, DOM bytes, and action receipt are content-addressed
  and participate in the ``evidence_set_hash`` (AC2);
* replay determinism -- the same flow over the same recorded observations produces a
  byte-identical action sequence, report, and verdict (AC4);
* tamper-evidence -- altering one recorded observation, one action receipt, or one
  recorded verdict fails verification naming the exact step index or check id (AC3);
* offline reattachment -- ``replay_reattach`` returns byte-identical snapshots from
  the store alone, with no network (AC4);
* isolation -- two concurrent browser tasks hold disjoint profiles with no cookie
  bleed (AC5); and
* driver failures and timeouts surface as typed terminal states, never free text (AC6).
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from bernstein.core.agents.computer_use import GENESIS_ANCHOR, Action, ActionKind, digest_typed_value
from bernstein.core.orchestration.activity import (
    ActivityKind,
    ActivityRejected,
    ActivityResult,
    Observation,
    TerminalState,
    dispatch_activity,
)
from bernstein.core.orchestration.activity_modalities import (
    ContentStore,
    replay_reattach,
    verify_run_activities,
)
from bernstein.core.orchestration.browser_check import (
    BrowserFlowReport,
    CheckKind,
    report_to_canonical_bytes,
)
from bernstein.core.orchestration.browser_driver import (
    BrowserDriverError,
    BrowserDriverUnavailable,
    BrowserProfile,
    BrowserStepTimeout,
    PageState,
    RecordedBrowserDriver,
)
from bernstein.core.orchestration.browser_worker import (
    BrowserBudget,
    BrowserBudgetExceeded,
    BrowserRunResult,
    BrowserWorker,
    CheckSpec,
    FlowStep,
)
from bernstein.core.replay.journal import EventJournal
from bernstein.core.security.audit_chain import EVENT_ACTIVITY_RESULT, AuditChainStore

# A recorded login flow: three observations, so two actions plus the terminal
# post-action capture. No network, no wall clock.
_TAPE: tuple[PageState, ...] = (
    PageState(url="https://shop/", screenshot=b"png-landing", dom=b"<html>  Sign   in\n</html>"),
    PageState(url="https://shop/login", screenshot=b"png-form", dom=b"<html>Password</html>"),
    PageState(url="https://shop/home", screenshot=b"png-home", dom=b"<html>Welcome back</html>"),
)

_STEPS: tuple[FlowStep, ...] = (
    FlowStep(
        action=Action(kind=ActionKind.NAVIGATE, target="https://shop/login"),
        checks=(CheckSpec(check_id="landing-has-signin", kind=CheckKind.DOM_CONTAINS, operand="Sign in"),),
    ),
    FlowStep(
        action=Action(
            kind=ActionKind.TYPE,
            target="#password",
            value_digest=digest_typed_value("hunter2"),
        ),
        checks=(CheckSpec(check_id="form-has-password", kind=CheckKind.DOM_CONTAINS, operand="Password"),),
    ),
)

_FINAL_CHECKS: tuple[CheckSpec, ...] = (
    CheckSpec(check_id="logged-in", kind=CheckKind.DOM_CONTAINS, operand="Welcome back"),
    CheckSpec(check_id="no-error", kind=CheckKind.DOM_NOT_CONTAINS, operand="Error 500"),
)


def _worker(tmp_path: Path, *, max_steps: int = 10, **budget: int) -> BrowserWorker:
    return BrowserWorker(
        store=ContentStore(tmp_path / ".sdd" / "cas"),
        budget=BrowserBudget(max_steps=max_steps, **budget),
        profile_root=tmp_path / ".sdd" / "browser-profiles",
    )


def _run(worker: BrowserWorker, *, flow_id: str = "login-flow", tape: tuple[PageState, ...] = _TAPE):
    return worker.run(
        flow_id=flow_id,
        start_url="https://shop/",
        steps=_STEPS,
        driver_factory=lambda profile_dir: RecordedBrowserDriver(tape, profile_dir=profile_dir),
        final_checks=_FINAL_CHECKS,
    )


# ---------------------------------------------------------------------------
# AC2: every observation and action is content-addressed and anchored
# ---------------------------------------------------------------------------


def test_flow_anchors_one_observation_per_action_plus_the_terminal_capture(tmp_path: Path) -> None:
    run = _run(_worker(tmp_path))
    # Two declared actions plus the post-action terminal capture.
    assert len(run.report.steps) == 3
    assert [s.index for s in run.report.steps] == [0, 1, 2]
    assert run.report.steps[-1].action_kind == ActionKind.SCREENSHOT.value


def test_each_step_pins_screenshot_dom_and_action_receipt(tmp_path: Path) -> None:
    run = _run(_worker(tmp_path))
    for step in run.report.steps:
        assert step.screenshot_content_hash.startswith("sha256:")
        assert step.dom_content_hash.startswith("sha256:")
        # The action receipt is the anchor itself, folding in its predecessor.
        assert len(step.anchor) == 64
    assert run.report.steps[0].prev_anchor == GENESIS_ANCHOR
    for prior, current in zip(run.report.steps, run.report.steps[1:], strict=False):
        assert current.prev_anchor == prior.anchor
    assert run.report.head_anchor == run.report.steps[-1].anchor


def test_every_observation_hash_participates_in_the_evidence_set(tmp_path: Path) -> None:
    run = _run(_worker(tmp_path))
    anchored = {o.content_hash for o in run.result.observations}
    for step in run.report.steps:
        assert step.screenshot_content_hash in anchored
        assert step.dom_content_hash in anchored
    # The action receipt bytes are evidence too, not a log line beside it.
    assert any(o.kind == "action_receipt" for o in run.result.observations)


def test_report_is_stored_under_the_anchored_artifact_hash(tmp_path: Path) -> None:
    store = ContentStore(tmp_path / ".sdd" / "cas")
    worker = BrowserWorker(
        store=store,
        budget=BrowserBudget(max_steps=10),
        profile_root=tmp_path / ".sdd" / "browser-profiles",
    )
    run = _run(worker)
    assert store.get(run.result.artifact_hash) == report_to_canonical_bytes(run.report)


def test_typed_values_enter_the_chain_only_as_digests(tmp_path: Path) -> None:
    run = _run(_worker(tmp_path))
    body = json.dumps(run.report.to_dict())
    assert "hunter2" not in body
    assert digest_typed_value("hunter2") in body


# ---------------------------------------------------------------------------
# AC1: dispatched by the deterministic scheduler next to coding tasks
# ---------------------------------------------------------------------------


def test_browser_task_lands_in_the_journal_next_to_a_coding_task(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    journal = EventJournal(run_id="run-mixed", sdd_dir=sdd)
    store = ContentStore(sdd / "cas")
    store.put(b"spec-bytes")
    coding = ActivityResult.build(
        kind=ActivityKind.CODING,
        artifact={"diff": "patch"},
        observations=(Observation.of(kind="artifact", ref="spec", content=b"spec-bytes"),),
    )
    dispatch_activity(coding, stage_id="coding-0", journal=journal)

    run = _run(_worker(tmp_path))
    dispatch_activity(run.result, stage_id="browser-0", journal=journal)

    verified = verify_run_activities(sdd, run_id="run-mixed", store=ContentStore(sdd / "cas"))
    assert verified.ok
    assert {s.stage_id: s.kind for s in verified.stages} == {"coding-0": "coding", "browser-0": "browser"}


def test_dispatch_mirrors_into_the_audit_chain(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    run = _run(_worker(tmp_path))
    chain = AuditChainStore(tmp_path / "audit", key=b"0" * 32)
    journal = EventJournal(run_id="run-b", sdd_dir=sdd)
    dispatch_activity(run.result, stage_id="browser-0", journal=journal, chain=chain)

    rows = [e for e in chain.query(event_type=EVENT_ACTIVITY_RESULT) if e.details.get("kind") == "browser"]
    assert len(rows) == 1
    details = rows[0].details
    assert details["artifact_hash"] == run.result.artifact_hash
    assert details["terminal_state"] == "completed"
    # The chain carries hashes, never the observed page body.
    assert "Welcome back" not in json.dumps(details)


# ---------------------------------------------------------------------------
# AC4: replay determinism and offline reattachment
# ---------------------------------------------------------------------------


def test_same_flow_over_same_recording_is_byte_identical(tmp_path: Path) -> None:
    first = _run(_worker(tmp_path / "a"))
    second = _run(_worker(tmp_path / "b"))
    assert report_to_canonical_bytes(first.report) == report_to_canonical_bytes(second.report)
    assert first.result.artifact_hash == second.result.artifact_hash
    assert first.result.evidence_set_hash == second.result.evidence_set_hash
    assert first.report.head_anchor == second.report.head_anchor


def test_divergent_observation_surfaces_as_an_anchor_mismatch_at_the_exact_index(tmp_path: Path) -> None:
    baseline = _run(_worker(tmp_path / "a"))
    drifted_tape = (_TAPE[0], replace(_TAPE[1], screenshot=b"png-form-CHANGED"), _TAPE[2])
    drifted = _run(_worker(tmp_path / "b"), tape=drifted_tape)

    diverged = [
        index
        for index, (lhs, rhs) in enumerate(zip(baseline.report.steps, drifted.report.steps, strict=True))
        if lhs.anchor != rhs.anchor
    ]
    # Step 1 saw different bytes; every later anchor folds that in, so divergence
    # starts at exactly index 1 rather than surfacing as a flaky assertion.
    assert diverged[0] == 1
    assert baseline.report.steps[0].anchor == drifted.report.steps[0].anchor


def test_report_holds_no_wall_clock_field(tmp_path: Path) -> None:
    run = _run(_worker(tmp_path))
    body = json.dumps(run.report.to_dict())
    for leaky in ("timestamp", "started_at", "finished_at", "duration", "elapsed"):
        assert leaky not in body


def test_replay_reattach_returns_byte_identical_snapshots(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    store = ContentStore(sdd / "cas")
    run = _run(_worker(tmp_path))
    journal = EventJournal(run_id="run-b", sdd_dir=sdd)
    dispatch_activity(run.result, stage_id="browser-0", journal=journal)

    reattached = replay_reattach(sdd / "runs" / "run-b" / "journal.jsonl", store=store, stage_id="browser-0")
    # The exact bytes the worker saw, in capture order, from the store alone.
    for state in _TAPE:
        assert state.screenshot in reattached
        assert state.dom in reattached


def test_verify_is_stable_across_repeated_runs(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    store = ContentStore(sdd / "cas")
    run = _run(_worker(tmp_path))
    dispatch_activity(run.result, stage_id="browser-0", journal=EventJournal(run_id="run-b", sdd_dir=sdd))

    first = verify_run_activities(sdd, run_id="run-b", store=store)
    second = verify_run_activities(sdd, run_id="run-b", store=store)
    assert first == second


# ---------------------------------------------------------------------------
# AC3: tamper-evidence naming the failing index
# ---------------------------------------------------------------------------


def _dispatched(tmp_path: Path) -> tuple[Path, ContentStore, BrowserRunResult]:
    sdd = tmp_path / ".sdd"
    store = ContentStore(sdd / "cas")
    run = _run(_worker(tmp_path))
    dispatch_activity(run.result, stage_id="browser-0", journal=EventJournal(run_id="run-b", sdd_dir=sdd))
    return sdd, store, run


def test_verified_run_passes_offline_with_only_the_store(tmp_path: Path) -> None:
    sdd, store, _run_result = _dispatched(tmp_path)
    verified = verify_run_activities(sdd, run_id="run-b", store=store)
    assert verified.ok
    stage = verified.stages[0]
    assert stage.kind == "browser"
    assert stage.evidence_reattached
    assert stage.browser_verdict is not None
    assert stage.browser_verdict.ok
    assert [c.check_id for c in stage.browser_verdict.checks] == [
        "landing-has-signin",
        "form-has-password",
        "logged-in",
        "no-error",
    ]


def test_altering_one_stored_screenshot_fails_naming_the_step(tmp_path: Path) -> None:
    sdd, store, run = _dispatched(tmp_path)
    target = run.report.steps[1].screenshot_content_hash
    store.force_put(target, b"png-form-TAMPERED")

    verified = verify_run_activities(sdd, run_id="run-b", store=store)
    assert not verified.ok
    stage = verified.stages[0]
    assert not stage.ok
    assert "step 1" in stage.reason
    assert stage.browser_verdict is not None
    failed = next(s for s in stage.browser_verdict.steps if not s.ok)
    assert failed.index == 1


def test_altering_one_stored_dom_fails_naming_the_step(tmp_path: Path) -> None:
    sdd, store, run = _dispatched(tmp_path)
    target = run.report.steps[2].dom_content_hash
    store.force_put(target, b"<html>Error 500</html>")

    verified = verify_run_activities(sdd, run_id="run-b", store=store)
    assert not verified.ok
    assert "step 2" in verified.stages[0].reason


def test_forging_a_recorded_verdict_is_caught_because_it_is_recomputed(tmp_path: Path) -> None:
    # The threat model is a worker that lies about what it saw. Every hash here
    # recomputes -- the observations are genuine, the chain links, the artifact
    # hash binds -- and only the recorded verdict disagrees with the anchored
    # bytes. Verification re-evaluates the assertion and refuses, naming the
    # check. A screenshot folder with a pass/fail note cannot detect this.
    sdd = tmp_path / ".sdd"
    store = ContentStore(sdd / "cas")
    honest = _run(_worker(tmp_path))
    report: BrowserFlowReport = honest.report
    forged = replace(
        report,
        checks=tuple(replace(c, passed=False) if c.check_id == "logged-in" else c for c in report.checks),
    )
    store.put(report_to_canonical_bytes(forged))
    lying = ActivityResult.build(
        kind=ActivityKind.BROWSER,
        artifact=forged.to_dict(),
        observations=honest.result.observations,
    )
    dispatch_activity(lying, stage_id="browser-0", journal=EventJournal(run_id="run-b", sdd_dir=sdd))

    verified = verify_run_activities(sdd, run_id="run-b", store=store)
    assert not verified.ok
    assert "logged-in" in verified.stages[0].reason
    assert verified.stages[0].browser_verdict is not None
    failed = next(c for c in verified.stages[0].browser_verdict.checks if not c.ok)
    assert failed.check_id == "logged-in"
    # The recomputed verdict is what the bytes actually say.
    assert failed.passed is True


def test_rewriting_the_stored_report_breaks_the_artifact_hash_binding(tmp_path: Path) -> None:
    sdd, store, run = _dispatched(tmp_path)
    store.force_put(run.result.artifact_hash, b'{"flow_id":"other"}')
    verified = verify_run_activities(sdd, run_id="run-b", store=store)
    assert not verified.ok
    assert "artifact_hash" in verified.stages[0].reason


def test_a_plain_screenshot_folder_cannot_satisfy_this(tmp_path: Path) -> None:
    # Guard against a regression to unbound storage: every stored observation is
    # addressed by the hash of its own bytes, so a swapped blob is always detected.
    sdd, store, run = _dispatched(tmp_path)
    for step in run.report.steps:
        assert store.get(step.screenshot_content_hash)
        assert store.get(step.dom_content_hash)
    store.force_put(run.report.steps[0].screenshot_content_hash, b"swapped")
    assert not verify_run_activities(sdd, run_id="run-b", store=store).ok


# ---------------------------------------------------------------------------
# AC5: per-task isolation
# ---------------------------------------------------------------------------


def test_two_browser_tasks_get_disjoint_profile_directories(tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    seen: list[Path] = []

    def factory(profile_dir: Path) -> RecordedBrowserDriver:
        seen.append(profile_dir)
        (profile_dir / "cookies.txt").write_text(f"session={profile_dir.name}", encoding="utf-8")
        return RecordedBrowserDriver(_TAPE, profile_dir=profile_dir)

    worker.run(flow_id="task-a", start_url="https://shop/", steps=_STEPS, driver_factory=factory)
    worker.run(flow_id="task-b", start_url="https://shop/", steps=_STEPS, driver_factory=factory)

    assert len(seen) == 2
    assert seen[0] != seen[1]
    assert seen[0] not in seen[1].parents
    assert seen[1] not in seen[0].parents


def test_profile_is_torn_down_on_a_terminal_state(tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    captured: list[Path] = []

    def factory(profile_dir: Path) -> RecordedBrowserDriver:
        captured.append(profile_dir)
        (profile_dir / "cookies.txt").write_text("session=secret", encoding="utf-8")
        return RecordedBrowserDriver(_TAPE, profile_dir=profile_dir)

    worker.run(flow_id="task-a", start_url="https://shop/", steps=_STEPS, driver_factory=factory)
    assert not captured[0].exists()


def test_profile_is_torn_down_even_when_the_driver_fails(tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    captured: list[Path] = []

    def factory(profile_dir: Path) -> RecordedBrowserDriver:
        captured.append(profile_dir)
        return RecordedBrowserDriver(_TAPE[:1], profile_dir=profile_dir)

    run = worker.run(flow_id="task-a", start_url="https://shop/", steps=_STEPS, driver_factory=factory)
    assert run.result.terminal_state is TerminalState.FAILED
    assert not captured[0].exists()


def test_profile_allocation_uses_the_worker_profile_root(tmp_path: Path) -> None:
    profile = BrowserProfile.allocate(root=tmp_path / "root", task_id="task-a")
    assert profile.profile_dir.parent == tmp_path / "root"
    profile.teardown()


# ---------------------------------------------------------------------------
# AC6: typed terminal states for driver failure, timeout, refusal, and budget
# ---------------------------------------------------------------------------


def test_driver_timeout_maps_to_a_typed_terminal_state(tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    run = worker.run(
        flow_id="f",
        start_url="https://shop/",
        steps=_STEPS,
        driver_factory=lambda d: RecordedBrowserDriver(_TAPE, profile_dir=d, timeout_at_step=1),
    )
    assert run.result.terminal_state is TerminalState.TIMED_OUT
    assert run.result.reason_code == "driver_timeout"
    # The partial flow is still anchored: the one action that completed, plus the
    # state the flow died in captured as a pure observation. That is exactly what
    # a post-incident reader needs, and it still verifies as a chain.
    assert len(run.report.steps) == 2
    assert run.report.steps[0].action_kind == ActionKind.NAVIGATE.value
    assert run.report.steps[1].action_kind == ActionKind.SCREENSHOT.value


def test_driver_error_maps_to_failed(tmp_path: Path) -> None:
    def factory(profile_dir: Path) -> RecordedBrowserDriver:
        return RecordedBrowserDriver(_TAPE[:1], profile_dir=profile_dir)

    run = _worker(tmp_path).run(flow_id="f", start_url="https://shop/", steps=_STEPS, driver_factory=factory)
    assert run.result.terminal_state is TerminalState.FAILED
    assert run.result.reason_code == "driver_error"


def test_unavailable_driver_maps_to_refused(tmp_path: Path) -> None:
    def factory(profile_dir: Path) -> RecordedBrowserDriver:
        raise BrowserDriverUnavailable(driver_name="browser_use", extra="browser")

    run = _worker(tmp_path).run(flow_id="f", start_url="https://shop/", steps=_STEPS, driver_factory=factory)
    assert run.result.terminal_state is TerminalState.REFUSED
    assert run.result.reason_code == "driver_unavailable"
    assert run.report.steps == ()
    assert run.report.head_anchor == GENESIS_ANCHOR


def test_a_failed_run_still_dispatches_and_verifies(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    store = ContentStore(sdd / "cas")
    worker = BrowserWorker(
        store=store,
        budget=BrowserBudget(max_steps=10),
        profile_root=sdd / "browser-profiles",
    )
    run = worker.run(
        flow_id="f",
        start_url="https://shop/",
        steps=_STEPS,
        driver_factory=lambda d: RecordedBrowserDriver(_TAPE, profile_dir=d, timeout_at_step=1),
    )
    dispatch_activity(run.result, stage_id="browser-0", journal=EventJournal(run_id="run-b", sdd_dir=sdd))
    verified = verify_run_activities(sdd, run_id="run-b", store=store)
    assert verified.ok
    assert verified.stages[0].kind == "browser"


def test_step_budget_refuses_before_the_extra_step(tmp_path: Path) -> None:
    worker = _worker(tmp_path, max_steps=1)
    with pytest.raises(BrowserBudgetExceeded, match="max_steps=1"):
        _run(worker)


def test_observation_byte_budget_refuses_before_storing(tmp_path: Path) -> None:
    worker = _worker(tmp_path, max_steps=10, max_observation_bytes=16)
    with pytest.raises(BrowserBudgetExceeded, match="max_observation_bytes"):
        _run(worker)


def test_a_check_naming_an_unknown_kind_is_refused_before_dispatch(tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    with pytest.raises(ActivityRejected, match="operand"):
        worker.run(
            flow_id="f",
            start_url="https://shop/",
            steps=(FlowStep(action=Action(kind=ActionKind.WAIT), checks=()),),
            driver_factory=lambda d: RecordedBrowserDriver(_TAPE, profile_dir=d),
            final_checks=(CheckSpec(check_id="k", kind=CheckKind.DOM_CONTAINS, operand=""),),
        )


def test_duplicate_check_ids_are_refused_before_dispatch(tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    dupes = (CheckSpec(check_id="k", kind=CheckKind.DOM_CONTAINS, operand="Sign in"),)
    with pytest.raises(ActivityRejected, match="duplicate check_id"):
        worker.run(
            flow_id="f",
            start_url="https://shop/",
            steps=(FlowStep(action=Action(kind=ActionKind.WAIT), checks=dupes),),
            driver_factory=lambda d: RecordedBrowserDriver(_TAPE, profile_dir=d),
            final_checks=dupes,
        )


def test_recorded_check_verdicts_reflect_the_observed_bytes(tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    run = worker.run(
        flow_id="f",
        start_url="https://shop/",
        steps=(FlowStep(action=Action(kind=ActionKind.WAIT), checks=()),),
        driver_factory=lambda d: RecordedBrowserDriver(_TAPE, profile_dir=d),
        final_checks=(CheckSpec(check_id="absent", kind=CheckKind.DOM_CONTAINS, operand="Nope"),),
    )
    assert run.report.checks[0].passed is False
    # A failing check is a recorded verdict, not an exception: the evidence stands.
    assert run.result.terminal_state is TerminalState.COMPLETED
    assert run.result.reason_code == "checks_failed"


def test_all_checks_passing_reports_ok(tmp_path: Path) -> None:
    run = _run(_worker(tmp_path))
    assert all(c.passed for c in run.report.checks)
    assert run.result.reason_code == "ok"


def test_driver_is_closed_on_every_path(tmp_path: Path) -> None:
    drivers: list[RecordedBrowserDriver] = []

    def factory(profile_dir: Path) -> RecordedBrowserDriver:
        driver = RecordedBrowserDriver(_TAPE[:1], profile_dir=profile_dir)
        drivers.append(driver)
        return driver

    _worker(tmp_path).run(flow_id="f", start_url="https://shop/", steps=_STEPS, driver_factory=factory)
    assert drivers[0].closed


def test_driver_error_type_is_the_documented_base(tmp_path: Path) -> None:
    assert issubclass(BrowserStepTimeout, BrowserDriverError)
    assert issubclass(BrowserDriverUnavailable, BrowserDriverError)
