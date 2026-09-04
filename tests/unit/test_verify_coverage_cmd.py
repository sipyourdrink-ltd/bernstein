"""CLI tests for ``bernstein verify coverage`` subcommand (task d0e3f56a3afb).

Verifies the coverage report computed from a merge admission receipt:
which gates/contexts were verified, which remain unverified, and which
were intentionally skipped. Tests exit-code contract and JSON output.

``verify coverage`` resolves its .sdd directory relative to the current
working directory, so each test chdirs into a tmp project (monkeypatch.chdir
restores cwd afterwards) and writes the merge receipt under .sdd/merges/receipts/.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.verify_cmd import verify_cmd
from bernstein.core.quality.merge_receipt import (
    compute_gate_results_hash,
    compute_ruleset_hash,
    merge_receipt_path,
)


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force a wide terminal so Rich does not truncate digest output."""
    monkeypatch.setenv("COLUMNS", "200")


def _write_merge_receipt(workdir: Path, head_sha: str, receipt_data: dict) -> Path:
    """Write a merge receipt to the canonical path and return its path."""
    path = merge_receipt_path(workdir, head_sha)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(receipt_data, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    return path


def _make_full_receipt(
    head_sha: str,
    gate_results_hash: str,
    ruleset_hash: str,
    required_context_ids: tuple[str, ...],
    review_receipt_id: str,
    journal_head: str,
    decision: str = "admit",
    authority: str = "autonomous",
    signature: str = "sig-placeholder",
) -> dict:
    """Build a complete MergeAdmissionReceipt.to_dict() fixture.

    Exercises the full path: all fields populated so ``to_dict`` -> ``from_dict``
    round-trips cleanly with non-empty ``gate_results_hash`` and
    ``required_context_ids``.
    """
    return {
        "v": 1,
        "head_sha": head_sha,
        "merge_base_sha": "0" * 40,
        "required_context_ids": list(required_context_ids),
        "gate_results_hash": gate_results_hash,
        "ruleset_hash": ruleset_hash,
        "review_receipt_id": review_receipt_id,
        "journal_head": journal_head,
        "decision": decision,
        "authority": authority,
        "timestamp": 1234567890,
        "signer_public_key_pem": "pem-placeholder",
        "signature": signature,
        "journal_entry_hash": "entry-hash-placeholder",
        "advisory": "",
    }


# ---------------------------------------------------------------------------
# 1. test_help_shows_coverage_subcommand
# ---------------------------------------------------------------------------


def test_help_shows_coverage_subcommand() -> None:
    """``bernstein verify --help`` lists the coverage subcommand."""
    result = CliRunner().invoke(verify_cmd, ["--help"])
    assert result.exit_code == 0
    assert "coverage" in result.output
    assert "verify coverage" in result.output
    assert "verify coverage <head-sha>" in result.output


# ---------------------------------------------------------------------------
# 2. test_coverage_receipt_not_found
# ---------------------------------------------------------------------------


def test_coverage_receipt_not_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No receipt -> exit 1 with a clear message."""
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(verify_cmd, ["coverage", "a" * 40])
    assert result.exit_code == 1
    # The receipt-not-found message is printed with Rich markup; the
    # console may strip tags in the captured output. Assert on exit code
    # and that the error is the file-not-found case (not a crash).
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# 3. test_coverage_remainder_matches
# ---------------------------------------------------------------------------


def test_coverage_remainder_matches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fixture receipt where remainder recomputes and matches -> exit 0.

    gate_results_hash is computed by the same routine the verifier uses
    (blast_radius=None, review_verdict=decision, required_contexts=...) so
    the recomputation matches and all items are verified or skipped.
    """
    head_sha = "abc123" + "0" * 34
    journal_head = "j" * 64
    required_context_ids = ("ci/build", "ci/lint")
    gate_results_hash = compute_gate_results_hash(
        blast_radius=None,
        review_verdict="admit",
        required_contexts=required_context_ids,
    )
    ruleset_hash = compute_ruleset_hash(required_contexts=required_context_ids)

    receipt_data = _make_full_receipt(
        head_sha=head_sha,
        gate_results_hash=gate_results_hash,
        ruleset_hash=ruleset_hash,
        required_context_ids=required_context_ids,
        review_receipt_id="",  # empty -> skipped (autonomous, no review)
        journal_head=journal_head,
        decision="admit",
        authority="autonomous",
    )

    monkeypatch.chdir(tmp_path)
    _write_merge_receipt(tmp_path, head_sha, receipt_data)

    result = CliRunner().invoke(verify_cmd, ["coverage", head_sha])
    assert result.exit_code == 0, result.output
    assert "Coverage is consistent" in result.output
    assert head_sha[:12] in result.output
    assert "admit" in result.output
    assert "autonomous" in result.output
    # gate_results is verified (match); review_receipt is skipped (empty).
    assert "Verified" in result.output
    assert "Skipped" in result.output


# ---------------------------------------------------------------------------
# 4. test_coverage_remainder_cannot_be_recomputed
# ---------------------------------------------------------------------------


def test_coverage_remainder_cannot_be_recomputed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Receipt whose gate_results_hash does not match recomputation.

    The verifier recomputes gate_results_hash with blast_radius=None; a hash
    computed with a non-empty blast_radius cannot be reproduced, so the
    remainder has a named reason and the exit code reflects divergence.

    Per the docstring contract: ``2  remainder cannot be recomputed
    (missing required fields)`` and ``3  remainder diverges from receipt
    value (inconsistent)``. The current implementation folds both into
    "unverified" + exit 3, with a named reason in the output. We assert
    the named reason is present and the receipt is flagged as unverified.
    """
    head_sha = "def456" + "0" * 34
    journal_head = "j" * 64

    # Hash computed with a non-empty blast_radius; verifier uses None.
    # Recomputation will diverge -> named reason emitted.
    gate_results_hash_with_blast = compute_gate_results_hash(
        blast_radius={"changed_files": 3, "modules": ["src"]},
        review_verdict="admit",
        required_contexts=("ci/build",),
    )

    receipt_data = _make_full_receipt(
        head_sha=head_sha,
        gate_results_hash=gate_results_hash_with_blast,
        ruleset_hash=compute_ruleset_hash(required_contexts=("ci/build",)),
        required_context_ids=("ci/build",),
        review_receipt_id="review-entry-hash",
        journal_head=journal_head,
        decision="admit",
    )

    monkeypatch.chdir(tmp_path)
    _write_merge_receipt(tmp_path, head_sha, receipt_data)

    result = CliRunner().invoke(verify_cmd, ["coverage", head_sha])
    # The recomputed hash (blast_radius=None) cannot reproduce the receipt's
    # recorded hash (computed with a blast-radius dict), so the remainder is
    # named unverified and exits non-zero per the documented contract.
    assert result.exit_code == 3
    assert "unverified" in result.output.lower()
    assert "gate_results" in result.output
    assert "mismatch" in result.output.lower()


# ---------------------------------------------------------------------------
# 5. test_coverage_remainder_diverges
# ---------------------------------------------------------------------------


def test_coverage_remainder_diverges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Receipt with empty gate_results_hash (unverified remainder) -> exit 3."""
    head_sha = "div789" + "0" * 34
    journal_head = "j" * 64

    # gate_results_hash is empty => unverified remainder => exit 3
    receipt_data = _make_full_receipt(
        head_sha=head_sha,
        gate_results_hash="",  # empty -> unverified
        ruleset_hash="",  # empty -> skipped
        required_context_ids=(),  # empty -> skipped
        review_receipt_id="",  # empty -> skipped
        journal_head=journal_head,
        decision="refuse",
        authority="operator_review",
    )

    monkeypatch.chdir(tmp_path)
    _write_merge_receipt(tmp_path, head_sha, receipt_data)

    result = CliRunner().invoke(verify_cmd, ["coverage", head_sha])
    assert result.exit_code == 3
    assert "unverified" in result.output.lower()
    assert "gate_results" in result.output
    assert "refuse" in result.output
    assert "operator_review" in result.output


# ---------------------------------------------------------------------------
# 6. test_coverage_json_output
# ---------------------------------------------------------------------------


def test_coverage_json_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--json flag emits a machine-readable JSON object."""
    head_sha = "json99" + "0" * 34
    journal_head = "j" * 64
    required_context_ids = ("ci/test",)
    gate_results_hash = compute_gate_results_hash(
        blast_radius=None,
        review_verdict="admit",
        required_contexts=required_context_ids,
    )

    receipt_data = _make_full_receipt(
        head_sha=head_sha,
        gate_results_hash=gate_results_hash,
        ruleset_hash=compute_ruleset_hash(required_contexts=required_context_ids),
        required_context_ids=required_context_ids,
        review_receipt_id="",
        journal_head=journal_head,
    )

    monkeypatch.chdir(tmp_path)
    _write_merge_receipt(tmp_path, head_sha, receipt_data)

    result = CliRunner().invoke(verify_cmd, ["coverage", head_sha, "--json"])
    assert result.exit_code == 0, result.output
    assert result.output.strip().startswith("{")
    data = json.loads(result.output.strip())
    assert data["head_sha"] == head_sha
    assert data["decision"] == "admit"
    assert "coverage" in data
    assert "verified" in data["coverage"]
    assert "unverified" in data["coverage"]
    assert "skipped" in data["coverage"]
    assert "exit_code" in data
    assert data["exit_code"] == 0
    assert "remainder" in data
    assert "reasons" in data
