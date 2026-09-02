"""Integrity and independence vectors (#5062).

Only question 17 lands here: it is the worked example that fixes the
shape for the other twenty. Its rule, which every later vector inherits:
answer the question from the bundle, under an interpreter that has
neither the product nor a network, or do not claim to have answered it.

The remaining vectors of this group are #5062.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from tests.integration.conformance.auditor import scenario
from tests.integration.conformance.auditor.isolation import RECEIPT_VERIFIER, run_isolated

if TYPE_CHECKING:
    from pathlib import Path

    from tests.integration.conformance.auditor.bundle_reader import BundleReader

# The isolated interpreter is built once per session; creating it is the
# expensive part of this module, not the verification.
pytestmark = pytest.mark.slow


class TestTheVerifyingInterpreterIsAStranger:
    """What the auditor's machine has, and what it does not."""

    def test_the_verifying_interpreter_cannot_import_bernstein(self, isolated_python: Path) -> None:
        """A verifier that leans on the product cannot pass a vector here."""
        proc = run_isolated(isolated_python, "-c", "import bernstein")
        assert proc.returncode != 0, "the verifying interpreter must not have bernstein installed"
        assert "No module named" in proc.stderr, proc.stderr

    def test_the_verifying_interpreter_cannot_open_a_socket(self, isolated_python: Path) -> None:
        """Offline is enforced, so a network call cannot pass unnoticed."""
        proc = run_isolated(isolated_python, "-c", "import socket; socket.socket()")
        assert proc.returncode != 0
        assert "NetworkDenied" in proc.stderr, proc.stderr


class TestQuestion17:
    """Can the bundle be verified with no network and no bernstein install?"""

    @pytest.mark.auditor_question(17)
    def test_the_bundle_can_be_verified_with_no_network_and_no_bernstein_install(
        self,
        auditor_bundle: BundleReader,
        isolated_python: Path,
    ) -> None:
        """The standalone verifier validates the bundle's receipt, alone."""
        receipt = auditor_bundle.path(scenario.AUDIT_RECEIPT_NAME)
        proc = run_isolated(isolated_python, str(RECEIPT_VERIFIER), "--receipt", str(receipt), "--verbose")

        assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        assert "OVERALL: PASS" in proc.stdout
        # Every envelope the receipt carries has to verify, not just one:
        # a receipt that passes on its weakest format answers nothing.
        assert "[PASS] cose" in proc.stdout
        assert "[PASS] intoto" in proc.stdout
        assert "[PASS] transparency" in proc.stdout
        assert "[PASS] subject_binding" in proc.stdout

    def test_an_edited_event_makes_the_offline_verifier_refuse_the_bundle(
        self,
        auditor_bundle: BundleReader,
        isolated_python: Path,
        tmp_path: Path,
    ) -> None:
        """The pass above is load-bearing: change one event and it goes red."""
        receipt = json.loads(auditor_bundle.read_bytes(scenario.AUDIT_RECEIPT_NAME).decode("utf-8"))
        assert receipt["events"], "the receipt embeds the events it attests"
        receipt["events"][0]["actor"] = "mallory"
        tampered = tmp_path / "tampered-receipt.json"
        tampered.write_text(json.dumps(receipt), encoding="utf-8")

        proc = run_isolated(isolated_python, str(RECEIPT_VERIFIER), "--receipt", str(tampered), "--verbose")

        assert proc.returncode == 1, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        assert "OVERALL: FAIL" in proc.stdout
        assert "[FAIL] subject_binding" in proc.stdout
