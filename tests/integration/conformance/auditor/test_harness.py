"""The instrument's own guarantees.

These are not vectors - they answer none of the 21 questions. They prove
the three properties that make a vector's answer mean anything: the
evidence came from a run rather than from a keyboard, the reader cannot
see past the bundle, and the question inventory is the fixed denominator
the scoreboard divides by.
"""

from __future__ import annotations

import json
import zipfile
from typing import TYPE_CHECKING

import pytest

from tests.integration.conformance.auditor import scenario
from tests.integration.conformance.auditor.bundle_reader import BundleBoundaryError, BundleReader
from tests.integration.conformance.auditor.questions import QUESTION_COUNT, QUESTIONS

if TYPE_CHECKING:
    from pathlib import Path

    from tests.integration.conformance.auditor.scenario import ScenarioFixture


class TestTheFixtureCameFromARun:
    """The bundle is the output of the scenario, not of an editor."""

    def test_the_bundle_is_produced_by_running_the_scenario(
        self,
        auditor_fixture: ScenarioFixture,
        auditor_bundle: BundleReader,
    ) -> None:
        """Every exported artefact traces back to a writer the run drove."""
        # The run left a real per-run audit chain, journal and spine behind.
        sdd = auditor_fixture.workspace / ".sdd"
        assert (sdd / "runtime" / "audit" / f"{scenario.RUN_ID}.audit.jsonl").is_file()
        assert (sdd / "runs" / scenario.RUN_ID / "journal.jsonl").is_file()
        assert (sdd / "lineage" / scenario.RUN_ID / "spine.jsonl").is_file()

        # And the bundle's receipts are bound to that run, not to a literal.
        run_receipt = auditor_bundle.read_json(scenario.RUN_RECEIPT_NAME)
        assert run_receipt["run_id"] == scenario.RUN_ID
        assert run_receipt["journal"]["event_count"] >= 5
        assert run_receipt["spine"]["entry_count"] >= 1

    def test_the_bundle_holds_exactly_the_exported_artefacts(
        self,
        auditor_bundle: BundleReader,
    ) -> None:
        """The auditor is handed a closed set of files, and knows it."""
        assert auditor_bundle.names() == sorted(
            [
                scenario.ARTICLE12_NAME,
                scenario.AUDIT_RECEIPT_NAME,
                scenario.BUNDLE_MANIFEST_NAME,
                scenario.RUN_RECEIPT_NAME,
            ],
        )
        manifest = auditor_bundle.read_json(scenario.BUNDLE_MANIFEST_NAME)
        assert manifest["run_id"] == scenario.RUN_ID
        assert set(manifest["files"]) == {
            scenario.ARTICLE12_NAME,
            scenario.AUDIT_RECEIPT_NAME,
            scenario.RUN_RECEIPT_NAME,
        }

    def test_the_article12_pack_opens_from_inside_the_bundle(
        self,
        auditor_bundle: BundleReader,
    ) -> None:
        """The evidence pack is readable without leaving the bundle."""
        manifest = json.loads(
            auditor_bundle.read_zip_member(scenario.ARTICLE12_NAME, "manifest.json").decode("utf-8"),
        )
        assert manifest["run_id"] == scenario.RUN_ID


class TestTheReaderCannotSeePastTheBundle:
    """A vector that reaches the producing machine is not a vector."""

    def test_a_traversing_name_cannot_reach_the_workspace(
        self,
        auditor_bundle: BundleReader,
    ) -> None:
        """``..`` is refused, so ``.sdd/`` stays out of reach."""
        with pytest.raises(BundleBoundaryError):
            auditor_bundle.path("..")
        with pytest.raises(BundleBoundaryError):
            auditor_bundle.read_bytes("../workspace/.sdd/runs")

    def test_an_absolute_path_cannot_be_read_through_the_reader(
        self,
        auditor_bundle: BundleReader,
        auditor_fixture: ScenarioFixture,
    ) -> None:
        """An absolute path is a name the bundle does not contain."""
        journal = auditor_fixture.workspace / ".sdd" / "runs" / scenario.RUN_ID / "journal.jsonl"
        assert journal.is_file(), "the run really did leave a journal outside the bundle"
        with pytest.raises(BundleBoundaryError):
            auditor_bundle.read_bytes(str(journal))

    def test_a_symlink_planted_in_the_bundle_cannot_widen_it(
        self,
        tmp_path: Path,
        auditor_fixture: ScenarioFixture,
    ) -> None:
        """Containment is checked after symlink resolution, not before."""
        bundle_copy = tmp_path / "bundle"
        bundle_copy.mkdir()
        (bundle_copy / scenario.BUNDLE_MANIFEST_NAME).write_bytes(
            (auditor_fixture.bundle / scenario.BUNDLE_MANIFEST_NAME).read_bytes(),
        )
        (bundle_copy / "shortcut").symlink_to(auditor_fixture.workspace / ".sdd")
        reader = BundleReader(bundle_copy)
        with pytest.raises(BundleBoundaryError):
            reader.path("shortcut")

    def test_an_archive_member_that_escapes_its_zip_is_refused(
        self,
        tmp_path: Path,
    ) -> None:
        """A crafted member name cannot be used to address the filesystem."""
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        archive = bundle / scenario.ARTICLE12_NAME
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("manifest.json", "{}")
        reader = BundleReader(bundle)
        with pytest.raises(BundleBoundaryError):
            reader.read_zip_member(scenario.ARTICLE12_NAME, "../escaped.json")
        with pytest.raises(BundleBoundaryError):
            reader.read_zip_member(scenario.ARTICLE12_NAME, "/etc/passwd")


class TestTheQuestionInventory:
    """The denominator the scoreboard divides by is fixed and complete."""

    def test_the_inventory_registers_exactly_twenty_one_questions(self) -> None:
        """21 questions, numbered 1..21, each with text."""
        assert QUESTION_COUNT == 21
        assert sorted(QUESTIONS) == list(range(1, 22))
        assert all(text.endswith("?") for text in QUESTIONS.values())
