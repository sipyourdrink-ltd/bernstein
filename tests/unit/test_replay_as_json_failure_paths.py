"""``--as-json`` is honoured on every exit path, not just the successful one (#3996).

#3991 made ``export`` / ``publish`` / ``verify`` emit real payloads. It did
not cover the failure paths, which stayed prose. ``verify`` was the sharp
case: a receipt that verifies *false* emitted ``{ok: false, errors: [...]}``
and exited 1, while a *malformed* receipt emitted Rich prose and also exited
1 - same status, one parseable and one not, with no way to tell which you
were getting before you tried.

Driven through the real CLI rather than by calling the functions, because
the wrong-verb refusal lives in ``advanced_cmd``'s dispatcher and never
reaches ``replay_cmd`` at all. A test that called the functions directly
would miss the one refusal ``--as-json`` never had any form of.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "bernstein", *args],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def malformed_receipt(tmp_path: Path) -> Path:
    receipt = tmp_path / "notareceipt.tar"
    receipt.write_text("this is not a tar archive", encoding="utf-8")
    return receipt


class TestEveryRefusalEmitsJsonWhenAsJsonWasAccepted:
    """The invariant: if ``--as-json`` was accepted, every exit path emits JSON."""

    @pytest.mark.parametrize(
        ("label", "args", "expected_error", "expected_code"),
        [
            (
                # The refusal #3991 introduced and gave no machine-readable
                # form at all - the one most likely to be hit BY a script.
                "wrong verb",
                ("replay", "export", "nope", "--yes-i-want-to-publish"),
                "flag_not_applicable",
                2,
            ),
            ("export missing agent", ("replay", "export", "nope"), "agent_journal_missing", 2),
            (
                "publish missing agent",
                ("replay", "publish", "nope", "--yes-i-want-to-publish"),
                "agent_journal_missing",
                2,
            ),
            ("publish unconfirmed", ("replay", "publish", "nope"), "confirmation_required", 2),
        ],
        ids=["wrong_verb", "export_no_agent", "publish_no_agent", "publish_unconfirmed"],
    )
    def test_refusal_is_parseable_and_names_itself(
        self,
        tmp_path: Path,
        label: str,
        args: tuple[str, ...],
        expected_error: str,
        expected_code: int,
    ) -> None:
        proc = run_cli(*args, "--as-json", "--sdd-dir", str(tmp_path))
        payload = json.loads(proc.stdout)  # raises if prose leaked through
        assert payload["error"] == expected_error, label
        assert payload["detail"], "a discriminator with no detail is not actionable"
        assert proc.returncode == expected_code

    def test_verify_missing_receipt(self, tmp_path: Path) -> None:
        proc = run_cli("replay", "verify", str(tmp_path / "absent.tar"), "--as-json")
        payload = json.loads(proc.stdout)
        assert payload["error"] == "receipt_not_found"
        assert proc.returncode == 2

    def test_verify_malformed_receipt(self, malformed_receipt: Path) -> None:
        proc = run_cli("replay", "verify", str(malformed_receipt), "--as-json")
        payload = json.loads(proc.stdout)
        assert payload["error"] == "receipt_malformed"
        assert proc.returncode == 1


class TestTheDistinctionThatMotivatedThis:
    def test_a_malformed_receipt_is_told_apart_from_a_chain_that_does_not_verify(self, malformed_receipt: Path) -> None:
        """Both exit 1. The payload is what routes them to different people.

        "this chain does not verify" is a finding about the run; "this file is
        not a receipt" is a finding about the invocation. Before this change a
        caller could only tell them apart by catching a parse exception - which
        is the problem the flag exists to remove.
        """
        proc = run_cli("replay", "verify", str(malformed_receipt), "--as-json")
        payload = json.loads(proc.stdout)
        assert proc.returncode == 1
        # `error` present => the invocation was wrong. A chain that merely
        # fails to verify carries head_hash/steps/errors and NO `error` key.
        assert "error" in payload
        assert "head_hash" not in payload

    def test_verify_keeps_ok_across_both_outcomes(self, malformed_receipt: Path) -> None:
        # `ok` is already part of verify's success contract, so a client keyed
        # on data["ok"] degrades sensibly instead of raising KeyError.
        payload = json.loads(run_cli("replay", "verify", str(malformed_receipt), "--as-json").stdout)
        assert payload["ok"] is False

    def test_export_and_publish_do_not_invent_an_ok_key(self, tmp_path: Path) -> None:
        # They have no `ok` on success, so adding one on failure alone would
        # mean a key that exists only when things go wrong.
        for args in (("replay", "export", "nope"), ("replay", "publish", "nope")):
            payload = json.loads(run_cli(*args, "--as-json", "--sdd-dir", str(tmp_path)).stdout)
            assert "ok" not in payload, args


class TestProseIsUnchangedWithoutTheFlag:
    """The control. If these passed too, the refactor would have broken humans."""

    @pytest.mark.parametrize(
        ("args", "needle"),
        [
            (("replay", "export", "nope"), "No journal for agent"),
            (("replay", "publish", "nope"), "Refusing to publish"),
        ],
        ids=["export", "publish"],
    )
    def test_a_human_still_gets_a_sentence(self, tmp_path: Path, args: tuple[str, ...], needle: str) -> None:
        proc = run_cli(*args, "--sdd-dir", str(tmp_path))
        flat = " ".join(proc.stdout.split())
        assert needle in flat
        with pytest.raises(json.JSONDecodeError):
            json.loads(proc.stdout)
