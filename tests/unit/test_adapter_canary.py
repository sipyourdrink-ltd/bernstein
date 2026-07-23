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
    CanaryOutcome,
    CanaryTarget,
    apply_canary_outcome,
    build_canary_receipt,
    canary_issue_body,
    canary_issue_title,
    failure_fingerprint,
    load_canary_state,
    load_last_green,
    receipt_sha256,
    render_last_green_table,
    run_canary_target,
    run_matrix,
    save_canary_state,
    save_last_green,
    update_last_green,
    verify_canary_receipt,
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

    def test_missing_binary_is_skip(self, tmp_path: Path) -> None:
        target = CanaryTarget(adapter="agy", binary="agy", model="gemini-3.1-flash-lite")
        outcome = run_canary_target(target, which=lambda _n: None, contracts_dir=tmp_path)
        assert outcome.verdict == "skip"
        assert outcome.failures == ()

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

    def test_render_table_rows_sorted_by_adapter(self) -> None:
        entries = {}
        for name in ("gemini", "agy"):
            outcome = _outcome(adapter=name, verdict="pass", failures=())
            entries.update(update_last_green(entries, outcome, receipt_sha="cd" * 32, recorded_at=_GENERATED_AT))
        table = render_last_green_table(entries)
        assert table.index("| agy ") < table.index("| gemini ")
        assert "1.4.0" in table
        assert ("cd" * 32)[:12] in table  # receipt anchor visible in docs

    def test_write_last_green_doc_is_idempotent(self, tmp_path: Path) -> None:
        doc = tmp_path / "conformance-canary.md"
        doc.write_text(
            "# Canary\n\n<!-- last-green:begin -->\nstale\n<!-- last-green:end -->\ntail\n",
            encoding="utf-8",
        )
        outcome = _outcome(verdict="pass", failures=())
        entries = update_last_green({}, outcome, receipt_sha="ef" * 32, recorded_at=_GENERATED_AT)
        write_last_green_doc(doc, entries)
        once = doc.read_text(encoding="utf-8")
        write_last_green_doc(doc, entries)
        assert doc.read_text(encoding="utf-8") == once
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
        from bernstein.cli.commands import doctor_cmd

        def fake_which(binary: str) -> str | None:
            return "/usr/local/bin/agy" if binary == "agy" else None

        with (
            patch.object(doctor_cmd.shutil, "which", side_effect=fake_which),
            patch.object(doctor_cmd, "_probe_adapter_version", return_value="1.4.0"),
            patch("bernstein.adapters.canary.load_last_green", return_value=self._entries()),
        ):
            rows = doctor_cmd.check_canary_last_green()
        assert len(rows) == 1
        assert rows[0]["status"] == "PASS"

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
