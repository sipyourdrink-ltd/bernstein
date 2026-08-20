"""Tests for the adapter conformance canary matrix (nightly regression finder).

The canary exercises every primary adapter's conformance contract against
whatever upstream version is installed, and turns each probe into a
content-addressed receipt:

* the receipt's canonical bytes hash to its identity, so two runs that
  observed the same upstream surface at the same timestamp produce
  byte-identical receipts;
* a mutated receipt fails verification exactly like a tampered chain entry;
* the per-adapter last-green table (rendered into docs, read by doctor) is a
  projection of passing receipts, never a hand-maintained list.

Issue automation is threshold-gated (two consecutive failures) and deduped
on a failure fingerprint so one upstream flake never opens an issue and the
same regression never opens two.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from bernstein.adapters.canary import (
    CANARY_GOAL,
    CANARY_MATRIX,
    CANARY_SCHEMA_VERSION,
    FAILURE_ISSUE_THRESHOLD,
    LAST_GREEN_STALE_DAYS,
    SKIP_ISSUE_THRESHOLD,
    CanaryOutcome,
    CanaryTarget,
    LastGreenEntry,
    ReceiptSetError,
    apply_canary_outcome,
    build_canary_receipt,
    canary_issue_body,
    canary_issue_title,
    canary_skip_issue_body,
    canary_skip_issue_title,
    failure_fingerprint,
    load_canary_state,
    load_last_green,
    receipt_sha256,
    render_last_green_table,
    run_canary_target,
    run_matrix,
    save_canary_state,
    save_last_green,
    skip_fingerprint,
    update_last_green,
    verify_canary_receipt,
    verify_last_green_projection,
    write_canary_receipt,
    write_last_green_doc,
)

if TYPE_CHECKING:
    from types import ModuleType

_GENERATED_AT = "2026-07-11T00:00:00Z"

_ADAPTER_CANARY_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "adapter_canary.py"


def _load_adapter_canary_script() -> ModuleType:
    """Import ``scripts/adapter_canary.py`` as a module by file path.

    The nightly entrypoint lives outside the importable package, so it is
    loaded via an explicit spec. The module is cached in ``sys.modules``
    under a private name so repeated loads are cheap.
    """
    mod_name = "_adapter_canary_script_under_test"
    cached = sys.modules.get(mod_name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(mod_name, _ADAPTER_CANARY_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _outcome(
    *,
    adapter: str = "agy",
    verdict: str = "fail",
    version: str | None = "1.4.0",
    failures: tuple[str, ...] = ("required flag missing from --help: --output-format",),
    skip_reason: str | None = None,
) -> CanaryOutcome:
    return CanaryOutcome(
        adapter=adapter,
        binary=adapter,
        model="gemini-3.1-flash-lite",
        goal=CANARY_GOAL,
        installed_version=version,
        verdict=verdict,
        failures=failures if verdict == "fail" else (),
        transcript=(f"{adapter} --help: probe", "verdict: " + verdict),
        skip_reason=skip_reason if verdict == "skip" else None,
    )


def _write_stub_cli(bin_dir: Path, name: str, *, version: str, help_text: str) -> Path:
    """Write an executable stub CLI that answers --version and --help."""
    if sys.platform.startswith("win"):  # pragma: no cover
        pytest.skip("POSIX shell scripts required for canary probe stubs.")
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / name
    path.write_text(
        f'#!/bin/sh\nif [ "$1" = "--version" ]; then\n  echo "{name} {version}"\n  exit 0\nfi\necho "{help_text}"\n',
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _write_contract(contracts_dir: Path, name: str, flags: list[str], *, binary: Path) -> None:
    """Write a minimal contract whose help command targets the stub binary.

    The binary is pinned by absolute path so the probe is hermetic even on
    hosts where a real CLI with the same name is installed.
    """
    contracts_dir.mkdir(parents=True, exist_ok=True)
    lines = [f"adapter: {name}", f'binary: "{binary}"', "required_flags:"]
    lines.extend(f'  - "{flag}"' for flag in flags)
    lines.append("required_subcommands: []")
    (contracts_dir / f"{name}.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Matrix data
# ---------------------------------------------------------------------------


class TestCanaryMatrix:
    """The matrix is curated data: primary adapters, one pinned tiny goal."""

    def test_matrix_is_nonempty_and_deterministically_ordered(self) -> None:
        adapters = [t.adapter for t in CANARY_MATRIX]
        assert adapters == sorted(adapters)
        assert len(adapters) == len(set(adapters))

    def test_matrix_covers_agy_and_primary_adapters(self) -> None:
        adapters = {t.adapter for t in CANARY_MATRIX}
        assert {"agy", "gemini", "claude", "codex", "aider", "copilot"} <= adapters

    def test_every_target_names_registry_adapter(self) -> None:
        from bernstein.adapters.registry import _ADAPTERS

        for target in CANARY_MATRIX:
            assert target.adapter in _ADAPTERS

    def test_every_target_pins_model_and_goal(self) -> None:
        for target in CANARY_MATRIX:
            assert target.model
            assert target.binary
        assert CANARY_GOAL
        assert len(CANARY_GOAL) < 200  # one tiny fixed goal, bounded spend


# ---------------------------------------------------------------------------
# Single-target probe
# ---------------------------------------------------------------------------


class TestRunCanaryTarget:
    """Probe one adapter: version capture + conformance verdict."""

    def test_missing_binary_is_absent(self, tmp_path: Path) -> None:
        """Issue #3562: a missing binary is gated, not skipped.

        An uninstalled binary is a runner-environment fact, not an upstream
        conformance signal, so :func:`run_canary_target` returns
        ``verdict="absent"`` rather than ``"skip"`` (a ``"skip"`` would
        accumulate toward a skip-streak issue, which would page operators
        about a missing binary every SKIP_ISSUE_THRESHOLD nights -- noise).
        ``skip_reason`` is deliberately unset: the absent path is its own
        verdict and has no reason classification.
        """
        target = CanaryTarget(adapter="agy", binary="agy", model="gemini-3.1-flash-lite")
        outcome = run_canary_target(target, which=lambda _n: None, contracts_dir=tmp_path)
        assert outcome.verdict == "absent"
        assert outcome.failures == ()
        assert outcome.skip_reason is None
        # The transcript still carries the gated reason so the receipt is
        # reproducible on a runner that does ship the binary.
        assert any("not on PATH" in line for line in outcome.transcript)

    def test_conforming_binary_passes(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        stub = _write_stub_cli(
            bin_dir,
            "agy",
            version="1.4.0",
            help_text="usage: agy -p -m --output-format --sandbox --yolo --session-id",
        )
        contracts = tmp_path / "contracts"
        _write_contract(contracts, "agy", ["-p", "-m", "--output-format", "--sandbox", "--yolo"], binary=stub)
        target = CanaryTarget(adapter="agy", binary="agy", model="default")
        outcome = run_canary_target(target, which=lambda _n: str(stub), contracts_dir=contracts)
        assert outcome.verdict == "pass"
        assert outcome.installed_version == "1.4.0"
        assert outcome.failures == ()

    def test_broken_pinned_version_is_caught(self, tmp_path: Path) -> None:
        """AC: the canary catches an intentionally broken pinned version."""
        bin_dir = tmp_path / "bin"
        # Pinned broken build: --help no longer advertises --output-format,
        # exactly the contract-drift shape an upstream release can ship.
        stub = _write_stub_cli(
            bin_dir,
            "agy",
            version="1.5.0",
            help_text="usage: agy -p -m --yolo",
        )
        contracts = tmp_path / "contracts"
        _write_contract(contracts, "agy", ["-p", "-m", "--output-format", "--sandbox", "--yolo"], binary=stub)
        target = CanaryTarget(adapter="agy", binary="agy", model="default")
        outcome = run_canary_target(target, which=lambda _n: str(stub), contracts_dir=contracts)
        assert outcome.verdict == "fail"
        assert outcome.installed_version == "1.5.0"
        assert any("--output-format" in failure for failure in outcome.failures)
        assert outcome.transcript  # the failing transcript rides into the issue

    def test_help_advertising_no_required_tokens_is_skip_not_fail(self, tmp_path: Path) -> None:
        """Regression for issue #2488: a --help that advertises none of the
        contract's required tokens is a broken/redesigned probe, not drift.

        The reported regression saw all six aider flags marked "missing" at
        once and a misleading per-flag issue opened, even though the flags
        were still present. A binary that answers --help but advertises none
        of its declared surface must probe as ``skip`` (investigate) so no
        regression issue is opened; only a *partial* miss is genuine drift.
        """
        bin_dir = tmp_path / "bin"
        # A wholesale-redesigned help banner that advertises none of the six
        # aider flags, exactly the shape that produced #2488.
        stub = _write_stub_cli(
            bin_dir,
            "aider",
            version="3.13",
            help_text="aider 3.13 - run 'aider docs' for the new command surface.",
        )
        contracts = tmp_path / "contracts"
        _write_contract(
            contracts,
            "aider",
            ["--model", "--message", "--yes-always", "--auto-commits", "--map-tokens", "--no-auto-lint"],
            binary=stub,
        )
        target = CanaryTarget(adapter="aider", binary="aider", model="gpt-5-mini")
        outcome = run_canary_target(target, which=lambda _n: str(stub), contracts_dir=contracts)
        assert outcome.verdict == "skip"
        assert outcome.failures == ()
        assert any("none of" in line for line in outcome.transcript)
        # The skip carries a stable reason so a chronic skip streak can escalate.
        assert outcome.skip_reason is not None

    def test_skip_transcript_carries_resolved_binary_path(self, tmp_path: Path) -> None:
        """The resolved binary path rides into the skip transcript.

        An operator must be able to tell a shadowed/wrong binary on PATH
        from real drift: the receipt records exactly which file was probed.
        """
        bin_dir = tmp_path / "bin"
        stub = _write_stub_cli(bin_dir, "aider", version="3.13", help_text="aider 3.13 - no advertised surface")
        contracts = tmp_path / "contracts"
        _write_contract(contracts, "aider", ["--model", "--message"], binary=stub)
        target = CanaryTarget(adapter="aider", binary="aider", model="gpt-5-mini")
        outcome = run_canary_target(target, which=lambda _n: str(stub), contracts_dir=contracts)
        assert outcome.verdict == "skip"
        assert any(str(stub) in line for line in outcome.transcript)


# ---------------------------------------------------------------------------
# Receipts: canonical, content-addressed, tamper-evident
# ---------------------------------------------------------------------------


class TestCanaryReceipts:
    def test_receipt_is_deterministic(self) -> None:
        outcome = _outcome()
        first = build_canary_receipt(outcome, generated_at=_GENERATED_AT)
        second = build_canary_receipt(outcome, generated_at=_GENERATED_AT)
        assert first == second
        assert receipt_sha256(first) == receipt_sha256(second)

    def test_receipt_sha_changes_with_content(self) -> None:
        base = receipt_sha256(build_canary_receipt(_outcome(), generated_at=_GENERATED_AT))
        other = receipt_sha256(build_canary_receipt(_outcome(version="1.5.0"), generated_at=_GENERATED_AT))
        assert base != other

    def test_receipt_carries_schema_and_probe_fields(self) -> None:
        receipt = build_canary_receipt(_outcome(), generated_at=_GENERATED_AT)
        assert receipt["schema_version"] == CANARY_SCHEMA_VERSION
        assert receipt["adapter"] == "agy"
        assert receipt["verdict"] == "fail"
        assert receipt["installed_version"] == "1.4.0"
        assert receipt["generated_at"] == _GENERATED_AT
        assert receipt["goal"] == CANARY_GOAL

    def test_written_receipt_verifies_and_tamper_is_detected(self, tmp_path: Path) -> None:
        receipt = build_canary_receipt(_outcome(), generated_at=_GENERATED_AT)
        path = write_canary_receipt(tmp_path / "receipts", receipt)
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert verify_canary_receipt(doc)
        doc["receipt"]["verdict"] = "pass"  # forge the verdict
        assert not verify_canary_receipt(doc)

    def test_receipt_filename_is_content_addressed(self, tmp_path: Path) -> None:
        receipt = build_canary_receipt(_outcome(), generated_at=_GENERATED_AT)
        path = write_canary_receipt(tmp_path / "receipts", receipt)
        assert receipt_sha256(receipt)[:16] in path.name

    def test_hostile_adapter_name_rejected(self, tmp_path: Path) -> None:
        outcome = _outcome(adapter="../../etc/passwd")
        receipt = build_canary_receipt(outcome, generated_at=_GENERATED_AT)
        with pytest.raises(ValueError, match="adapter"):
            write_canary_receipt(tmp_path / "receipts", receipt)


# ---------------------------------------------------------------------------
# Failure threshold + issue dedupe
# ---------------------------------------------------------------------------


class TestFailureThresholdAndDedupe:
    def test_single_failure_never_opens_issue(self) -> None:
        state, should_open = apply_canary_outcome({}, _outcome())
        assert should_open is False
        assert state["agy"]["consecutive_failures"] == 1

    def test_two_consecutive_failures_open_issue(self) -> None:
        assert FAILURE_ISSUE_THRESHOLD == 2
        state, _ = apply_canary_outcome({}, _outcome())
        state, should_open = apply_canary_outcome(state, _outcome())
        assert should_open is True
        assert state["agy"]["consecutive_failures"] == 2

    def test_same_fingerprint_is_deduped_after_report(self) -> None:
        state, _ = apply_canary_outcome({}, _outcome())
        state, first = apply_canary_outcome(state, _outcome())
        state, second = apply_canary_outcome(state, _outcome())
        assert first is True
        assert second is False  # already reported, do not re-open

    def test_new_failure_fingerprint_reports_again(self) -> None:
        state, _ = apply_canary_outcome({}, _outcome())
        state, _ = apply_canary_outcome(state, _outcome())
        regressed_again = _outcome(version="1.6.0")
        state, _ = apply_canary_outcome(state, regressed_again)
        state, should_open = apply_canary_outcome(state, regressed_again)
        assert should_open is True

    def test_pass_resets_counter(self) -> None:
        state, _ = apply_canary_outcome({}, _outcome())
        state, _ = apply_canary_outcome(state, _outcome(verdict="pass", failures=()))
        state, should_open = apply_canary_outcome(state, _outcome())
        assert should_open is False
        assert state["agy"]["consecutive_failures"] == 1

    def test_skip_does_not_touch_counter(self) -> None:
        state, _ = apply_canary_outcome({}, _outcome())
        state, should_open = apply_canary_outcome(state, _outcome(verdict="skip", failures=(), version=None))
        assert should_open is False
        assert state["agy"]["consecutive_failures"] == 1

    def test_fingerprint_depends_on_version_and_failures(self) -> None:
        a = failure_fingerprint(_outcome())
        b = failure_fingerprint(_outcome(version="1.5.0"))
        c = failure_fingerprint(_outcome(failures=("other drift",)))
        assert len({a, b, c}) == 3

    def test_state_round_trips_through_disk(self, tmp_path: Path) -> None:
        state, _ = apply_canary_outcome({}, _outcome())
        path = tmp_path / "state" / "canary-state.json"
        save_canary_state(path, state)
        assert load_canary_state(path) == state

    def test_issue_title_and_body_carry_transcript(self) -> None:
        outcome = _outcome()
        title = canary_issue_title(outcome)
        body = canary_issue_body(
            outcome, receipt_sha=receipt_sha256(build_canary_receipt(outcome, generated_at=_GENERATED_AT))
        )
        assert "agy" in title
        assert "1.4.0" in title
        for line in outcome.transcript:
            assert line in body
        assert "--output-format" in body


# ---------------------------------------------------------------------------
# Skip-streak escalation
# ---------------------------------------------------------------------------


class TestSkipStreakEscalation:
    """A chronic same-reason skip must become visible, mirroring fails.

    A degraded (``skip``) probe is not a conformance break, but an adapter
    that skips for the same reason night after night is silently unverified
    -- exactly the blind spot the canary exists to close. The skip streak
    escalates on its own counter and threshold, distinct from the fail path.
    """

    def _skip(self, *, adapter: str = "aider", reason: str = "probe inconclusive (all_absent)") -> CanaryOutcome:
        return _outcome(adapter=adapter, verdict="skip", failures=(), version=None, skip_reason=reason)

    def test_single_skip_never_escalates(self) -> None:
        state, should_open = apply_canary_outcome({}, self._skip())
        assert should_open is False
        assert state["aider"]["consecutive_skips"] == 1

    def test_threshold_same_reason_skips_escalate_once(self) -> None:
        assert SKIP_ISSUE_THRESHOLD >= 3
        state: dict = {}
        opens: list[bool] = []
        for _ in range(SKIP_ISSUE_THRESHOLD):
            state, should_open = apply_canary_outcome(state, self._skip())
            opens.append(should_open)
        # Exactly one escalation, fired on the threshold-crossing night.
        assert opens.count(True) == 1
        assert opens[-1] is True
        assert state["aider"]["consecutive_skips"] == SKIP_ISSUE_THRESHOLD
        # A further same-reason skip does not re-open (deduped by fingerprint).
        state, again = apply_canary_outcome(state, self._skip())
        assert again is False

    def test_new_skip_reason_restarts_the_streak(self) -> None:
        state: dict = {}
        for _ in range(SKIP_ISSUE_THRESHOLD):
            state, _ = apply_canary_outcome(state, self._skip(reason="probe inconclusive (all_absent)"))
        # A new skip *reason* restarts the streak. The other valid skip
        # reason (``"conformance skip"``) is the catch-all emitted by
        # ``run_canary_target`` when the binary is on PATH but the
        # in-process contract check produces no detail string; the
        # previously-tested ``"binary not on PATH"`` reason no longer
        # produces a ``skip`` verdict at all (it is now ``absent`` -- see
        # :meth:`TestRunCanaryTarget.test_missing_binary_is_absent`), so it
        # does not appear here.
        state, should_open = apply_canary_outcome(state, self._skip(reason="conformance skip"))
        assert should_open is False
        assert state["aider"]["consecutive_skips"] == 1

    def test_pass_resets_skip_streak(self) -> None:
        state: dict = {}
        for _ in range(SKIP_ISSUE_THRESHOLD - 1):
            state, _ = apply_canary_outcome(state, self._skip())
        state, _ = apply_canary_outcome(state, _outcome(adapter="aider", verdict="pass", failures=()))
        assert state["aider"]["consecutive_skips"] == 0
        # After a reset it takes another full streak to escalate again.
        opens: list[bool] = []
        for _ in range(SKIP_ISSUE_THRESHOLD):
            state, should_open = apply_canary_outcome(state, self._skip())
            opens.append(should_open)
        assert opens.count(True) == 1

    def test_refuse_still_never_escalates(self) -> None:
        refusal = _outcome(adapter="aider", verdict="refuse", failures=(), version="0.1.0")
        state, should_open = apply_canary_outcome({}, refusal)
        assert should_open is False
        assert "consecutive_skips" not in state.get("aider", {})

    def test_skip_streak_does_not_touch_fail_counter(self) -> None:
        state, _ = apply_canary_outcome({}, _outcome())  # one fail for agy
        state, _ = apply_canary_outcome(state, self._skip(adapter="agy"))
        assert state["agy"]["consecutive_failures"] == 1

    def test_skip_fingerprint_depends_on_adapter_and_reason(self) -> None:
        a = skip_fingerprint(self._skip(adapter="aider", reason="probe inconclusive (all_absent)"))
        b = skip_fingerprint(self._skip(adapter="aider", reason="conformance skip"))
        c = skip_fingerprint(self._skip(adapter="agy", reason="probe inconclusive (all_absent)"))
        assert len({a, b, c}) == 3

    def test_skip_issue_title_and_body_are_distinct_from_regression(self) -> None:
        outcome = self._skip()
        title = canary_skip_issue_title(outcome)
        body = canary_skip_issue_body(
            outcome, receipt_sha=receipt_sha256(build_canary_receipt(outcome, generated_at=_GENERATED_AT))
        )
        assert "aider" in title
        assert "skip" in title.lower()
        # Must not read as a confirmed drift regression.
        assert "regression" not in title.lower()
        assert "probe inconclusive (all_absent)" in body

    def test_run_matrix_escalates_a_chronic_skip(self, tmp_path: Path) -> None:
        """A workflow-shaped run opens exactly one skip issue at the threshold."""
        bin_dir = tmp_path / "bin"
        # A stub whose --help advertises none of the required tokens is an
        # inconclusive skip (report.py), the aider blind-spot shape.
        stub = _write_stub_cli(bin_dir, "aider", version="0.86.2", help_text="usage: aider [options]")
        contracts = tmp_path / "contracts"
        _write_contract(contracts, "aider", ["--yes-always", "--message"], binary=stub)
        targets = (CanaryTarget(adapter="aider", binary="aider", model="gpt-5-mini"),)
        kwargs = {
            "receipts_dir": tmp_path / "receipts",
            "state_path": tmp_path / "state.json",
            "last_green_path": tmp_path / "last_green.json",
            "docs_path": None,
            "generated_at": _GENERATED_AT,
            "which": lambda _n: str(stub),
            "contracts_dir": contracts,
        }
        results = [run_matrix(targets, **kwargs) for _ in range(SKIP_ISSUE_THRESHOLD)]
        # Never a regression (advisory-green preserved), no fail escalation.
        assert all(r.regressions == [] for r in results)
        opened = [issue for r in results for issue in r.issues_to_open]
        assert len(opened) == 1
        assert "skip" in opened[0]["title"].lower()
        # The receipt still records the degraded probe.
        assert all(o.verdict == "skip" for r in results for o in r.outcomes)


# ---------------------------------------------------------------------------
# Absent-binary gate (#3562)
# ---------------------------------------------------------------------------


class TestAbsentBinaryGate:
    """Issue #3562: a missing binary is gated, not skipped.

    An uninstalled binary is a runner-environment fact, not an upstream
    conformance signal, so :func:`run_canary_target` emits
    ``verdict="absent"`` and :func:`apply_canary_outcome` must leave the
    per-adapter state untouched -- it must never count toward the failure
    or skip streaks, never file an issue, and never perturb a prior
    streak that is already in progress for a different reason.
    """

    def _absent(self, *, adapter: str = "aider") -> CanaryOutcome:
        return _outcome(adapter=adapter, verdict="absent", failures=(), version=None)

    def test_absent_never_opens_issue(self) -> None:
        # A missing binary on a runner that has never shipped it must not
        # page operators after SKIP_ISSUE_THRESHOLD nights (the previous
        # behavior, before #3562).
        state: dict = {}
        for _ in range(SKIP_ISSUE_THRESHOLD * 4):
            state, should_open = apply_canary_outcome(state, self._absent())
            assert should_open is False

    def test_absent_does_not_register_state_entry(self) -> None:
        """An absent adapter is invisible to apply_canary_outcome.

        The state mapping must remain empty for an adapter that has only
        ever been ``absent`` -- it has produced no signal worth persisting,
        so a later transition to ``fail`` or ``skip`` starts from a clean
        slate (just like an adapter the canary has never run for).
        """
        state, _ = apply_canary_outcome({}, self._absent())
        assert state == {}

    def test_absent_does_not_reset_prior_skip_streak(self) -> None:
        """An absent outcome is not a pass: it must not reset counters.

        A ``pass`` resets every counter because a green run is positive
        evidence that supersedes prior degradation. An ``absent`` outcome
        is no evidence at all (the probe did not run), so it must neither
        reset an in-progress skip streak nor reset an in-progress failure
        streak. The next real ``pass`` is the only thing that resets.
        """
        state: dict = {}
        for _ in range(SKIP_ISSUE_THRESHOLD - 1):
            state, _ = apply_canary_outcome(state, _outcome(adapter="aider", verdict="skip", failures=(), version=None))
        assert state["aider"]["consecutive_skips"] == SKIP_ISSUE_THRESHOLD - 1
        # Two absent runs in a row -- the skip streak must be unchanged.
        state, _ = apply_canary_outcome(state, self._absent())
        state, _ = apply_canary_outcome(state, self._absent())
        assert state["aider"]["consecutive_skips"] == SKIP_ISSUE_THRESHOLD - 1

    def test_absent_does_not_reset_prior_fail_streak(self) -> None:
        """Symmetric to the skip-streak case for the failure counter."""
        state, _ = apply_canary_outcome({}, _outcome())  # one fail for agy
        assert state["agy"]["consecutive_failures"] == 1
        state, _ = apply_canary_outcome(state, self._absent(adapter="agy"))
        assert state["agy"]["consecutive_failures"] == 1

    def test_absent_after_full_skip_streak_does_not_open_issue(self) -> None:
        """The streak that already escalated is the one that escalates.

        After a full skip streak has already opened its issue, a switch
        to absent (the binary was uninstalled on the runner that night)
        must not re-open or escalate -- and the already-reported skip
        fingerprint stays reported, so a switch back to skip with the
        same reason does not re-open either.
        """
        state: dict = {}
        for _ in range(SKIP_ISSUE_THRESHOLD):
            state, _ = apply_canary_outcome(
                state,
                _outcome(
                    adapter="aider",
                    verdict="skip",
                    failures=(),
                    version=None,
                    skip_reason="probe inconclusive (all_absent)",
                ),
            )
        # Runner uninstalls aider: nightly absent outcomes.
        for _ in range(SKIP_ISSUE_THRESHOLD * 2):
            state, should_open = apply_canary_outcome(state, self._absent())
            assert should_open is False
        # Skip fingerprint already reported, so a return to skip does not
        # re-open either.
        state, should_open = apply_canary_outcome(
            state,
            _outcome(
                adapter="aider",
                verdict="skip",
                failures=(),
                version=None,
                skip_reason="probe inconclusive (all_absent)",
            ),
        )
        assert should_open is False


# ---------------------------------------------------------------------------
# Last-green table
# ---------------------------------------------------------------------------


class TestLastGreen:
    def test_pass_updates_entry_with_receipt_anchor(self) -> None:
        outcome = _outcome(verdict="pass", failures=())
        sha = receipt_sha256(build_canary_receipt(outcome, generated_at=_GENERATED_AT))
        entries = update_last_green({}, outcome, receipt_sha=sha, recorded_at=_GENERATED_AT)
        entry = entries["agy"]
        assert entry.version == "1.4.0"
        assert entry.receipt_sha256 == sha
        assert entry.recorded_at == _GENERATED_AT

    def test_fail_and_skip_do_not_update(self) -> None:
        entries = update_last_green({}, _outcome(), receipt_sha="x", recorded_at=_GENERATED_AT)
        assert entries == {}
        entries = update_last_green(
            {}, _outcome(verdict="skip", failures=(), version=None), receipt_sha="x", recorded_at=_GENERATED_AT
        )
        assert entries == {}

    def test_round_trip_through_disk(self, tmp_path: Path) -> None:
        outcome = _outcome(verdict="pass", failures=())
        entries = update_last_green({}, outcome, receipt_sha="ab" * 32, recorded_at=_GENERATED_AT)
        path = tmp_path / "last_green.json"
        save_last_green(path, entries)
        loaded = load_last_green(path)
        assert loaded["agy"].version == "1.4.0"
        assert loaded["agy"].receipt_sha256 == "ab" * 32

    def test_packaged_default_loads(self) -> None:
        entries = load_last_green()
        assert isinstance(entries, dict)

    def test_packaged_table_survives_validation(self) -> None:
        """The shipped projection must satisfy the boundary it is read through.

        Validating rows is only an improvement if the real table passes it; a
        stricter loader that silently empties the packaged file would turn every
        ``doctor`` staleness check into a no-op.
        """
        entries = load_last_green()
        assert entries, "the packaged last-green table must load at least one row"
        for name, entry in entries.items():
            assert len(entry.receipt_sha256) == 64, name
            assert entry.version.strip(), name

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("version", None),
            ("version", ["1.4.0"]),
            ("version", ""),
            ("binary", None),
            ("binary", 7),
            ("receipt_sha256", None),
            ("receipt_sha256", 123),
            ("receipt_sha256", "not-a-hash"),
            ("receipt_sha256", "AB" * 32),
            ("recorded_at", None),
            ("recorded_at", {"at": "2026-07-11T05:57:23Z"}),
            ("recorded_at", "yesterday"),
        ],
    )
    def test_malformed_row_is_dropped_rather_than_coerced(self, tmp_path: Path, field: str, value: object) -> None:
        """A row that was never valid must not arrive looking like an attestation.

        ``str(value)`` renders ``None`` as ``"None"`` and a list as its repr, so
        a corrupt row used to load as a populated entry that admission and
        ``doctor`` then read as a receipt-backed claim.
        """
        row = {
            "binary": "agy",
            "version": "1.4.0",
            "receipt_sha256": "ab" * 32,
            "recorded_at": "2026-07-11T05:57:23Z",
        }
        row[field] = value  # type: ignore[assignment]
        path = tmp_path / "last_green.json"
        path.write_text(json.dumps({"adapters": {"agy": row}}), encoding="utf-8")

        assert load_last_green(path) == {}, f"{field}={value!r} must not load as an entry"

    def test_a_malformed_row_does_not_discard_its_valid_neighbours(self, tmp_path: Path) -> None:
        path = tmp_path / "last_green.json"
        path.write_text(
            json.dumps(
                {
                    "adapters": {
                        "agy": {
                            "binary": "agy",
                            "version": None,
                            "receipt_sha256": "ab" * 32,
                            "recorded_at": "2026-07-11T05:57:23Z",
                        },
                        "claude": {
                            "binary": "claude",
                            "version": "2.1.227",
                            "receipt_sha256": "cd" * 32,
                            "recorded_at": "2026-08-11T05:47:04Z",
                        },
                    }
                }
            ),
            encoding="utf-8",
        )

        loaded = load_last_green(path)

        assert set(loaded) == {"claude"}
        assert loaded["claude"].version == "2.1.227"

    def test_surrounding_whitespace_is_normalised_not_carried_into_consumers(self, tmp_path: Path) -> None:
        """A padded field passes an emptiness check and then breaks its consumer.

        ``shutil.which(" claude")`` finds nothing and a padded version reaches
        admission as a version nobody installed, so the row would produce a
        stale-or-unknown verdict about a perfectly current install.
        """
        path = tmp_path / "last_green.json"
        path.write_text(
            json.dumps(
                {
                    "adapters": {
                        "claude": {
                            "binary": " claude ",
                            "version": "\t2.1.227\n",
                            "receipt_sha256": "cd" * 32,
                            "recorded_at": "2026-08-11T05:47:04Z",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        loaded = load_last_green(path)

        assert loaded["claude"].binary == "claude"
        assert loaded["claude"].version == "2.1.227"

    def test_render_table_rows_sorted_by_adapter(self) -> None:
        entries = {}
        for name in ("gemini", "agy"):
            outcome = _outcome(adapter=name, verdict="pass", failures=())
            entries.update(update_last_green(entries, outcome, receipt_sha="cd" * 32, recorded_at=_GENERATED_AT))
        table = render_last_green_table(entries)
        assert table.index("| agy ") < table.index("| gemini ")
        assert "1.4.0" in table
        assert ("cd" * 32)[:12] in table  # receipt anchor visible in docs

    def test_render_table_marks_stale_rows(self) -> None:
        from datetime import UTC, datetime

        entries = update_last_green(
            {}, _outcome(verdict="pass", failures=()), receipt_sha="cd" * 32, recorded_at="2026-07-01T00:00:00Z"
        )
        # A "now" well past the staleness window flags the row.
        stale_now = datetime(2026, 7, 1, tzinfo=UTC).replace(day=1 + LAST_GREEN_STALE_DAYS + 5)
        table = render_last_green_table(entries, now=stale_now)
        assert "stale" in table.lower()
        # A "now" inside the window leaves the same row unmarked.
        fresh_now = datetime(2026, 7, 2, tzinfo=UTC)
        assert "stale" not in render_last_green_table(entries, now=fresh_now).lower()

    def test_write_last_green_doc_is_idempotent(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime

        doc = tmp_path / "conformance-canary.md"
        doc.write_text(
            "# Canary\n\n<!-- last-green:begin -->\nplaceholder\n<!-- last-green:end -->\ntail\n",
            encoding="utf-8",
        )
        outcome = _outcome(verdict="pass", failures=())
        entries = update_last_green({}, outcome, receipt_sha="ef" * 32, recorded_at=_GENERATED_AT)
        # A "now" inside the freshness window keeps the fixed row unmarked, so
        # the regeneration is a deterministic function of (entries, now).
        now = datetime(2026, 7, 12, tzinfo=UTC)
        write_last_green_doc(doc, entries, now=now)
        once = doc.read_text(encoding="utf-8")
        write_last_green_doc(doc, entries, now=now)
        assert doc.read_text(encoding="utf-8") == once
        assert "placeholder" not in once
        assert "stale" not in once
        assert "1.4.0" in once
        assert once.endswith("tail\n")

    def test_write_last_green_doc_requires_markers(self, tmp_path: Path) -> None:
        doc = tmp_path / "no-markers.md"
        doc.write_text("# no markers\n", encoding="utf-8")
        with pytest.raises(ValueError, match="marker"):
            write_last_green_doc(doc, {})

    def test_shipped_doc_carries_markers_and_table(self) -> None:
        from bernstein.adapters.canary import LAST_GREEN_DOC_PATH

        text = LAST_GREEN_DOC_PATH.read_text(encoding="utf-8")
        assert "<!-- last-green:begin -->" in text
        assert "<!-- last-green:end -->" in text


# ---------------------------------------------------------------------------
# Matrix runner (what the nightly workflow executes)
# ---------------------------------------------------------------------------


class TestRunMatrix:
    def test_broken_pinned_version_produces_issue_after_threshold(self, tmp_path: Path) -> None:
        """AC: a workflow-shaped run catches the intentionally broken pin."""
        bin_dir = tmp_path / "bin"
        stub = _write_stub_cli(bin_dir, "agy", version="1.5.0", help_text="usage: agy -p")
        contracts = tmp_path / "contracts"
        _write_contract(contracts, "agy", ["-p", "--output-format"], binary=stub)
        targets = (CanaryTarget(adapter="agy", binary="agy", model="default"),)
        kwargs = {
            "receipts_dir": tmp_path / "receipts",
            "state_path": tmp_path / "state.json",
            "last_green_path": tmp_path / "last_green.json",
            "docs_path": None,
            "generated_at": _GENERATED_AT,
            "which": lambda _n: str(stub),
            "contracts_dir": contracts,
        }
        first = run_matrix(targets, **kwargs)
        assert first.regressions == ["agy"]
        assert first.issues_to_open == []  # first strike: threshold not met
        second = run_matrix(targets, **kwargs)
        assert second.regressions == ["agy"]
        assert len(second.issues_to_open) == 1
        issue = second.issues_to_open[0]
        assert "agy" in issue["title"]
        assert "--output-format" in issue["body"]
        third = run_matrix(targets, **kwargs)
        assert third.issues_to_open == []  # deduped: same fingerprint already reported

    def test_green_run_updates_last_green_and_docs(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        stub = _write_stub_cli(bin_dir, "agy", version="1.4.0", help_text="usage: agy -p --output-format")
        contracts = tmp_path / "contracts"
        _write_contract(contracts, "agy", ["-p", "--output-format"], binary=stub)
        docs = tmp_path / "canary.md"
        docs.write_text("<!-- last-green:begin -->\n<!-- last-green:end -->\n", encoding="utf-8")
        targets = (CanaryTarget(adapter="agy", binary="agy", model="gemini-3.1-flash-lite"),)
        result = run_matrix(
            targets,
            receipts_dir=tmp_path / "receipts",
            state_path=tmp_path / "state.json",
            last_green_path=tmp_path / "last_green.json",
            docs_path=docs,
            generated_at=_GENERATED_AT,
            which=lambda _n: str(stub),
            contracts_dir=contracts,
        )
        assert result.regressions == []
        entries = load_last_green(tmp_path / "last_green.json")
        assert entries["agy"].version == "1.4.0"
        assert "1.4.0" in docs.read_text(encoding="utf-8")
        receipts = list((tmp_path / "receipts").glob("*.json"))
        assert len(receipts) == 1
        assert verify_canary_receipt(json.loads(receipts[0].read_text(encoding="utf-8")))

    def test_receipts_dir_containment_enforced(self, tmp_path: Path) -> None:
        if sys.platform.startswith("win"):  # pragma: no cover
            pytest.skip("symlink creation requires elevated privileges on Windows.")
        outside = tmp_path / "outside"
        link = tmp_path / "receipts"
        outside.mkdir()
        os.symlink(outside, link)
        receipt = build_canary_receipt(_outcome(), generated_at=_GENERATED_AT)
        # A symlinked receipts dir is resolved; the write must land inside
        # the resolved base, never escape through a stale component.
        path = write_canary_receipt(link, receipt)
        assert path.resolve().is_relative_to(outside.resolve())


# ---------------------------------------------------------------------------
# Audit chain mirror
# ---------------------------------------------------------------------------


class TestAuditChainMirror:
    def test_record_adapter_canary_receipt_appends_event(self, tmp_path: Path) -> None:
        from bernstein.core.security.audit_chain import (
            EVENT_ADAPTER_CANARY_RECEIPT,
            AuditChainStore,
            record_adapter_canary_receipt,
        )

        chain = AuditChainStore(tmp_path / "audit", key=b"0" * 32)
        event = record_adapter_canary_receipt(
            chain=chain,
            adapter="agy",
            binary="agy",
            installed_version="1.5.0",
            verdict="fail",
            receipt_sha256="ab" * 32,
            failures=["required flag missing from --help: --output-format"],
        )
        assert event.event_type == EVENT_ADAPTER_CANARY_RECEIPT
        rows = chain.query(event_type=EVENT_ADAPTER_CANARY_RECEIPT)
        assert len(rows) == 1
        details = rows[0].details
        assert details["adapter"] == "agy"
        assert details["verdict"] == "fail"
        assert details["receipt_sha256"] == "ab" * 32
        assert "prev_chain_digest" in details


# ---------------------------------------------------------------------------
# Nightly entrypoint anchors receipts into the HMAC audit chain (#2843)
# ---------------------------------------------------------------------------


class TestNightlyAnchorsReceipts:
    """The nightly path must anchor every receipt into the HMAC chain.

    ``scripts/adapter_canary.py`` is the only caller that drives the
    canary in CI. If it does not pass an ``AuditChainStore`` to
    ``run_matrix`` the receipts are self-hashed but never anchored, so
    the docstring/docs claim (receipt hashes mirrored into the HMAC
    audit chain) is false for the automated path.
    """

    def test_nightly_run_anchors_every_receipt_and_verifies(self, tmp_path: Path) -> None:
        from bernstein.core.security.audit_chain import (
            EVENT_ADAPTER_CANARY_RECEIPT,
            AuditChainStore,
        )

        script = _load_adapter_canary_script()

        bin_dir = tmp_path / "bin"
        good = _write_stub_cli(bin_dir, "agy", version="1.4.0", help_text="usage: agy -p --output-format")
        bad = _write_stub_cli(bin_dir, "claude", version="2.0.0", help_text="usage: claude -p")
        contracts = tmp_path / "contracts"
        _write_contract(contracts, "agy", ["-p", "--output-format"], binary=good)
        _write_contract(contracts, "claude", ["-p", "--output-format"], binary=bad)
        stubs = {"agy": str(good), "claude": str(bad)}

        def which(name: str) -> str | None:
            return stubs.get(name)

        targets = (
            CanaryTarget(adapter="agy", binary="agy", model="default"),
            CanaryTarget(adapter="claude", binary="claude", model="default"),
        )

        out_dir = tmp_path / "adapter-canary"
        key = b"K" * 32
        result = script.run_nightly_canary(
            targets,
            out_dir=out_dir,
            generated_at=_GENERATED_AT,
            which=which,
            contracts_dir=contracts,
            audit_key=key,
        )
        assert len(result.receipt_paths) == 2

        # The chain segment must be persisted under the receipts directory
        # so the existing "Upload receipts" artifact step captures it.
        chain_dir = out_dir / "receipts" / "audit-chain"
        assert chain_dir.is_dir()

        # Reopen the persisted chain with the same key and verify integrity.
        chain = AuditChainStore(chain_dir, key=key)
        ok, errors = chain.verify()
        assert ok, errors

        anchored = {row.details["receipt_sha256"] for row in chain.query(event_type=EVENT_ADAPTER_CANARY_RECEIPT)}
        assert len(anchored) == 2

        # REAL recompute: every sealed receipt file's content hash must be
        # present as an anchor in the persisted chain.
        for receipt_path in result.receipt_paths:
            doc = json.loads(receipt_path.read_text(encoding="utf-8"))
            assert verify_canary_receipt(doc)
            recomputed = receipt_sha256(doc["receipt"])
            assert recomputed in anchored

    def test_main_wires_audit_chain_into_run_matrix(self, tmp_path: Path) -> None:
        """The CLI ``main`` must route through the anchoring helper."""
        script = _load_adapter_canary_script()

        captured: dict[str, object] = {}

        def fake_run_nightly(targets: object, **kwargs: object) -> object:
            captured["kwargs"] = kwargs
            from bernstein.adapters.canary import MatrixRunResult

            return MatrixRunResult()

        with patch.object(script, "run_nightly_canary", side_effect=fake_run_nightly):
            rc = script.main(["--adapter", "agy", "--out-dir", str(tmp_path / "out")])

        assert rc == 0
        kwargs = captured["kwargs"]
        assert isinstance(kwargs, dict)
        assert "out_dir" in kwargs
        assert "generated_at" in kwargs


# ---------------------------------------------------------------------------
# Doctor: ahead-of-last-green advisory
# ---------------------------------------------------------------------------


class TestDoctorAheadOfLastGreen:
    def _entries(self) -> dict:
        outcome = _outcome(verdict="pass", failures=())
        return update_last_green({}, outcome, receipt_sha="ab" * 32, recorded_at=_GENERATED_AT)

    def test_installed_ahead_of_last_green_warns(self) -> None:
        from bernstein.cli.commands import doctor_cmd

        def fake_which(binary: str) -> str | None:
            return "/usr/local/bin/agy" if binary == "agy" else None

        with (
            patch.object(doctor_cmd.shutil, "which", side_effect=fake_which),
            patch.object(doctor_cmd, "_probe_adapter_version", return_value="1.5.0"),
            patch("bernstein.adapters.canary.load_last_green", return_value=self._entries()),
        ):
            rows = doctor_cmd.check_canary_last_green()
        assert len(rows) == 1
        row = rows[0]
        assert row["status"] == "WARN"
        assert "last-green" in row["detail"]
        assert "1.4.0" in row["detail"]

    def test_installed_at_last_green_passes(self) -> None:
        from datetime import UTC, datetime

        from bernstein.cli.commands import doctor_cmd

        def fake_which(binary: str) -> str | None:
            return "/usr/local/bin/agy" if binary == "agy" else None

        # Evaluate staleness as-of a "now" inside the freshness window so the
        # fixed _GENERATED_AT row is not flagged stale here.
        now = datetime(2026, 7, 12, tzinfo=UTC)
        with (
            patch.object(doctor_cmd.shutil, "which", side_effect=fake_which),
            patch.object(doctor_cmd, "_probe_adapter_version", return_value="1.4.0"),
            patch("bernstein.adapters.canary.load_last_green", return_value=self._entries()),
        ):
            rows = doctor_cmd.check_canary_last_green(now=now)
        assert len(rows) == 1
        assert rows[0]["status"] == "PASS"

    def test_installed_adapter_absent_from_last_green_warns(self) -> None:
        """An installed matrix adapter with no last-green row surfaces as WARN.

        Closes the "aider absent" blind spot: aider is a primary matrix
        adapter but carries no last-green row, so it never surfaced for a
        local operator -- not even as "no data".
        """
        from datetime import UTC, datetime

        from bernstein.cli.commands import doctor_cmd

        def fake_which(binary: str) -> str | None:
            return "/usr/local/bin/aider" if binary == "aider" else None

        now = datetime(2026, 7, 12, tzinfo=UTC)
        with (
            patch.object(doctor_cmd.shutil, "which", side_effect=fake_which),
            patch("bernstein.adapters.canary.load_last_green", return_value=self._entries()),
        ):
            rows = doctor_cmd.check_canary_last_green(now=now)
        aider_rows = [r for r in rows if "aider" in r["name"]]
        assert len(aider_rows) == 1
        assert aider_rows[0]["status"] == "WARN"
        assert "last-green" in aider_rows[0]["detail"].lower()

    def test_stale_last_green_row_warns(self) -> None:
        """A last-green row older than the staleness window surfaces as WARN.

        Closes the "agy stale" blind spot: agy's row froze weeks ago and
        read as automation-fresh with no marker.
        """
        from datetime import UTC, datetime

        from bernstein.cli.commands import doctor_cmd

        def fake_which(binary: str) -> str | None:
            return "/usr/local/bin/agy" if binary == "agy" else None

        # _GENERATED_AT is 2026-07-11; evaluate well past the window.
        now = datetime(2026, 8, 1, tzinfo=UTC)
        with (
            patch.object(doctor_cmd.shutil, "which", side_effect=fake_which),
            patch.object(doctor_cmd, "_probe_adapter_version", return_value="1.4.0"),
            patch("bernstein.adapters.canary.load_last_green", return_value=self._entries()),
        ):
            rows = doctor_cmd.check_canary_last_green(now=now)
        agy_rows = [r for r in rows if "agy" in r["name"]]
        assert len(agy_rows) == 1
        assert agy_rows[0]["status"] == "WARN"
        assert "stale" in agy_rows[0]["detail"].lower()

    def test_missing_binary_omitted(self) -> None:
        from bernstein.cli.commands import doctor_cmd

        with (
            patch.object(doctor_cmd.shutil, "which", return_value=None),
            patch("bernstein.adapters.canary.load_last_green", return_value=self._entries()),
        ):
            rows = doctor_cmd.check_canary_last_green()
        assert rows == []

    def test_unknown_version_omitted(self) -> None:
        from bernstein.cli.commands import doctor_cmd

        def fake_which(binary: str) -> str | None:
            return "/usr/local/bin/agy" if binary == "agy" else None

        with (
            patch.object(doctor_cmd.shutil, "which", side_effect=fake_which),
            patch.object(doctor_cmd, "_probe_adapter_version", return_value=None),
            patch("bernstein.adapters.canary.load_last_green", return_value=self._entries()),
        ):
            rows = doctor_cmd.check_canary_last_green()
        assert rows == []

    def test_wired_into_doctor_check_list(self) -> None:
        import inspect

        from bernstein.cli.commands import doctor_cmd

        source = inspect.getsource(doctor_cmd)
        assert "check_canary_last_green()" in source

    def test_surfaced_by_bernstein_doctor_cli(self) -> None:
        """The `bernstein doctor` surface (status_cmd) shows the advisory."""
        from bernstein.cli.commands import doctor_cmd, status_cmd

        def fake_which(binary: str) -> str | None:
            return "/usr/local/bin/agy" if binary == "agy" else None

        checks: list[dict] = []
        with (
            patch.object(doctor_cmd.shutil, "which", side_effect=fake_which),
            patch.object(doctor_cmd, "_probe_adapter_version", return_value="1.5.0"),
            patch("bernstein.adapters.canary.load_last_green", return_value=self._entries()),
        ):
            status_cmd._doctor_check_last_green(checks)
        assert len(checks) == 1
        assert checks[0]["ok"] is True  # advisory only: never fails doctor
        assert "WARNING" in checks[0]["detail"]
        assert "last-green" in checks[0]["detail"]

    def test_wired_into_bernstein_doctor_command(self) -> None:
        import inspect

        from bernstein.cli.commands import status_cmd

        source = inspect.getsource(status_cmd)
        assert "_doctor_check_last_green(checks)" in source


class TestVerifyLastGreenProjection:
    """#3940: nothing re-verified last_green.json against the receipts it projects.

    The docs table and the JSON are both written by one run, so checking them
    against each other proves they share a generator, not that the generator
    was right. These cover the faults that reproduce identically into both.
    """

    def _doc(self, adapter: str, *, verdict: str = "pass", generated_at: str = _GENERATED_AT) -> dict:
        receipt = build_canary_receipt(
            _outcome(adapter=adapter, verdict=verdict, failures=()),
            generated_at=generated_at,
        )
        return {"receipt": receipt, "receipt_sha256": receipt_sha256(receipt)}

    def _entry(self, doc: dict, *, recorded_at: str | None = None, digest: str | None = None) -> LastGreenEntry:
        receipt = doc["receipt"]
        return LastGreenEntry(
            adapter=receipt["adapter"],
            binary=receipt["binary"],
            version=receipt["installed_version"],
            receipt_sha256=digest if digest is not None else doc["receipt_sha256"],
            recorded_at=recorded_at if recorded_at is not None else receipt["generated_at"],
        )

    def test_matching_receipt_set_and_projection_passes(self) -> None:
        docs = [self._doc("agy"), self._doc("claude")]
        entries = {d["receipt"]["adapter"]: self._entry(d) for d in docs}

        assert verify_last_green_projection(docs, entries) == []

    def test_stale_recorded_at_is_rejected_as_carried_forward(self) -> None:
        doc = self._doc("agy")
        entries = {"agy": self._entry(doc, recorded_at="2026-06-01T00:00:00Z")}

        mismatches = verify_last_green_projection([doc], entries)

        assert [m.kind for m in mismatches] == ["stale_row"]
        assert mismatches[0].adapter == "agy"

    def test_entry_missing_backing_receipt_is_rejected(self) -> None:
        # claude's row claims this run, but only agy produced a receipt.
        doc = self._doc("agy")
        entries = {
            "agy": self._entry(doc),
            "claude": LastGreenEntry(
                adapter="claude",
                binary="claude",
                version="1.0.0",
                receipt_sha256="cd" * 32,
                recorded_at=_GENERATED_AT,
            ),
        }

        mismatches = verify_last_green_projection([doc], entries)

        assert [m.kind for m in mismatches] == ["missing_entry"]
        assert mismatches[0].adapter == "claude"

    def test_entry_with_wrong_digest_is_rejected(self) -> None:
        doc = self._doc("agy")
        entries = {"agy": self._entry(doc, digest="ff" * 32)}

        mismatches = verify_last_green_projection([doc], entries)

        assert [m.kind for m in mismatches] == ["wrong_digest"]

    def test_dropped_adapter_without_entry_is_rejected(self) -> None:
        docs = [self._doc("agy"), self._doc("claude")]
        entries = {"agy": self._entry(docs[0])}

        mismatches = verify_last_green_projection(docs, entries)

        assert [m.kind for m in mismatches] == ["dropped_adapter"]
        assert mismatches[0].adapter == "claude"

    def test_an_untouched_older_row_is_not_flagged(self) -> None:
        # agy is five weeks behind the rest in the real file and droid has no
        # row at all, both legitimately. A check demanding a receipt per row
        # would be permanently red against production data.
        doc = self._doc("claude")
        entries = {
            "claude": self._entry(doc),
            "agy": LastGreenEntry(
                adapter="agy",
                binary="agy",
                version="0.9.0",
                receipt_sha256="ab" * 32,
                recorded_at="2026-06-01T00:00:00Z",
            ),
        }

        assert verify_last_green_projection([doc], entries) == []

    def test_a_failing_receipt_does_not_require_a_row(self) -> None:
        # Only a pass advances the projection, so a fail with no row is right.
        docs = [self._doc("agy", verdict="fail")]

        assert verify_last_green_projection(docs, {}) == []

    def test_a_receipt_set_spanning_two_runs_is_refused(self) -> None:
        # Picking one stamp (the max, say) would make every row from the other
        # run look stale or fresh by accident: a verdict from an assumption.
        docs = [self._doc("agy"), self._doc("claude", generated_at="2026-07-12T00:00:00Z")]

        with pytest.raises(ReceiptSetError, match="spans 2 generated_at"):
            verify_last_green_projection(docs, {})

    def test_a_tampered_receipt_is_reported_not_trusted(self) -> None:
        doc = self._doc("agy")
        doc["receipt"]["installed_version"] = "9.9.9"

        mismatches = verify_last_green_projection([doc], {})

        assert [m.kind for m in mismatches] == ["unverifiable_receipt"]
