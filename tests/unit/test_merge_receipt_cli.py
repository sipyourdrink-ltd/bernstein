"""Test the merge-admission receipt CLI commands.

Issue #3754. Tests for ``bernstein merge verify`` and the related
merge-receipt creation/emission path.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.merge_cmd import merge_cmd
from bernstein.core.quality.merge_receipt import (
    emit_merge_receipt,
    read_merge_receipt,
    verify_merge_receipt,
)


@pytest.fixture(scope="function")
def workdir(tmp_path):
    """Create a temporary project root with .sdd directory."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".sdd").mkdir(parents=True)
    (root / ".sdd" / "identity").mkdir(parents=True)
    (root / ".sdd" / "lineage").mkdir(parents=True)
    (root / ".sdd" / "merges" / "receipts").mkdir(parents=True)
    return root


@pytest.fixture(scope="function")
def populated_workdir(workdir):
    """Create a working directory with signed merge identity."""
    from bernstein.core.quality.merge_receipt import load_or_create_merge_identity

    root = workdir
    private_pem, public_pem = load_or_create_merge_identity(root)
    identity_dir = root / ".sdd" / "identity"
    (identity_dir / "merge-identity-key.pem").write_text(private_pem, encoding="ascii")
    (identity_dir / "merge-identity-public.pem").write_text(public_pem, encoding="ascii")
    return root


def _emit(root, head_sha, merge_base_sha, **kwargs):
    """Helper to emit a merge receipt into an already-set-up workdir."""
    hmac_key = b"x" * 32
    lineage_root = root / ".sdd" / "lineage"
    private_key_pem = (root / ".sdd" / "identity" / "merge-identity-key.pem").read_text(encoding="ascii")
    public_key_pem = (root / ".sdd" / "identity" / "merge-identity-public.pem").read_text(encoding="ascii")

    defaults = dict(
        required_context_ids=("status/green",),
        blast_radius={
            "score": 0.2,
            "hard_one_way": False,
            "components": [],
            "hits": [],
            "rationale": "no destructive detectors fired",
            "files_touched": 0,
            "files": [],
        },
        review_verdict="pass",
        ruleset_bytes=b"",
        decision="admit",
        authority="autonomous",
        timestamp=1000,
    )
    defaults.update(kwargs)
    return emit_merge_receipt(
        workdir=root,
        lineage_root=lineage_root,
        hmac_key=hmac_key,
        private_key_pem=private_key_pem,
        public_key_pem=public_key_pem,
        head_sha=head_sha,
        merge_base_sha=merge_base_sha,
        **defaults,
    )


# -------------------------------------------------------------------
# emit + verify round-trip
# -------------------------------------------------------------------


def test_emit_and_verify_merge_receipt(populated_workdir):
    """Integration test: emit a merge receipt and verify it offline."""
    root = populated_workdir

    head_sha = "integration123"
    merge_base_sha = "base456"

    _emit(
        root,
        head_sha,
        merge_base_sha,
        required_context_ids=("unit/green", "integration/green"),
        blast_radius={
            "score": 0.3,
            "hard_one_way": False,
            "components": [],
            "hits": [],
            "rationale": "all gates passed",
            "files_touched": 5,
            "files": ["src/", "tests/"],
        },
        review_verdict="approve",
        decision="admit",
        authority="autonomous",
        timestamp=4000,
    )

    read_receipt = read_merge_receipt(root, head_sha)
    assert read_receipt is not None
    assert read_receipt.head_sha == head_sha
    assert read_receipt.decision == "admit"

    hmac_key = b"x" * 32
    verify_result = verify_merge_receipt(
        workdir=root,
        lineage_root=root / ".sdd" / "lineage",
        hmac_key=hmac_key,
        head_sha=head_sha,
    )

    assert verify_result.ok is True
    assert verify_result.receipt is not None
    assert verify_result.receipt.head_sha == head_sha


# -------------------------------------------------------------------
# verify: no receipt
# -------------------------------------------------------------------


def test_verify_no_receipt(workdir):
    """When no receipt exists, verify reports a failure with reason."""
    root = workdir
    hmac_key = b"x" * 32

    head_sha = "abc123"

    result = verify_merge_receipt(
        workdir=root,
        lineage_root=root / ".sdd" / "lineage",
        hmac_key=hmac_key,
        head_sha=head_sha,
    )

    assert result.ok is False
    assert result.receipt is None
    assert "no merge receipt found" in result.reason


# -------------------------------------------------------------------
# verify: stored refusal still verifies
# -------------------------------------------------------------------


def test_verify_stored_refusal(populated_workdir):
    """A receipt storing a refusal still verifies cryptographically."""
    root = populated_workdir
    hmac_key = b"x" * 32

    head_sha = "stored_refusal123"
    merge_base_sha = "base456"

    _emit(
        root,
        head_sha,
        merge_base_sha,
        required_context_ids=("integration/fail",),
        blast_radius={
            "score": 0.9,
            "hard_one_way": False,
            "components": [],
            "hits": [],
            "rationale": "integration failed",
            "files_touched": 3,
            "files": ["test_integration.py"],
        },
        review_verdict="fail",
        decision="refuse",
        authority="autonomous",
        timestamp=5000,
    )

    result = verify_merge_receipt(
        workdir=root,
        lineage_root=root / ".sdd" / "lineage",
        hmac_key=hmac_key,
        head_sha=head_sha,
    )

    assert result.ok is True  # Receipt is valid even though decision is refuse
    assert result.decision == "refuse"
    assert result.receipt is not None
    assert result.receipt.decision == "refuse"


# -------------------------------------------------------------------
# verify: hard one-way + advisory
# -------------------------------------------------------------------


def test_verify_hard_one_way_with_advisory(populated_workdir):
    """Verify a receipt where hard_one_way fired and an advisory was recorded."""
    root = populated_workdir
    hmac_key = b"x" * 32

    head_sha = "hard123"
    merge_base_sha = "def456"

    _emit(
        root,
        head_sha,
        merge_base_sha,
        required_context_ids=("status/red",),
        blast_radius={
            "score": 1.0,
            "hard_one_way": True,
            "components": [],
            "hits": [],
            "rationale": "hard one-way detector fired",
            "files_touched": 1,
            "files": ["secrets.py"],
        },
        review_verdict="fail",
        decision="refuse",
        authority="autonomous",
        advisory="Escalation: secrets file added",
        timestamp=2000,
    )

    result = verify_merge_receipt(
        workdir=root,
        lineage_root=root / ".sdd" / "lineage",
        hmac_key=hmac_key,
        head_sha=head_sha,
    )

    assert result.ok is True
    assert result.decision == "refuse"
    assert result.authority == "autonomous"
    assert result.receipt is not None
    assert result.receipt.advisory == "Escalation: secrets file added"


# -------------------------------------------------------------------
# verify: operator review authority
# -------------------------------------------------------------------


def test_verify_operator_review(populated_workdir):
    """Verify a receipt authored under operator-review authority."""
    root = populated_workdir
    hmac_key = b"x" * 32

    head_sha = "operator123"
    merge_base_sha = "def456"

    _emit(
        root,
        head_sha,
        merge_base_sha,
        required_context_ids=("build/green",),
        blast_radius={
            "score": 0.1,
            "hard_one_way": False,
            "components": [],
            "hits": [],
            "rationale": "build succeeded",
            "files_touched": 2,
            "files": ["src/", "tests/"],
        },
        review_verdict="pass",
        decision="admit",
        authority="operator_review",
        timestamp=3000,
    )

    result = verify_merge_receipt(
        workdir=root,
        lineage_root=root / ".sdd" / "lineage",
        hmac_key=hmac_key,
        head_sha=head_sha,
    )

    assert result.ok is True
    assert result.decision == "admit"
    assert result.authority == "operator_review"
    assert result.receipt is not None


# -------------------------------------------------------------------
# tamper detection
# -------------------------------------------------------------------


def test_verify_tamper_detected(populated_workdir):
    """A tampered receipt (decision changed after emit) fails verification."""
    root = populated_workdir
    hmac_key = b"x" * 32

    head_sha = "tamper123"
    merge_base_sha = "def456"

    _emit(
        root,
        head_sha,
        merge_base_sha,
        required_context_ids=("status/red",),
        blast_radius={
            "score": 0.9,
            "hard_one_way": False,
            "components": [],
            "hits": [],
            "rationale": "integration failed",
            "files_touched": 3,
            "files": ["test_integration.py"],
        },
        review_verdict="fail",
        decision="refuse",
        authority="autonomous",
        timestamp=5000,
    )

    # Tamper: flip the decision field in the stored JSON
    safe = hashlib.sha256(head_sha.encode("utf-8")).hexdigest()
    receipt_path = root / ".sdd" / "merges" / "receipts" / f"{safe}.json"
    data = json.loads(receipt_path.read_text(encoding="utf-8"))
    data["decision"] = "admit"  # was "refuse"
    receipt_path.write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )

    result = verify_merge_receipt(
        workdir=root,
        lineage_root=root / ".sdd" / "lineage",
        hmac_key=hmac_key,
        head_sha=head_sha,
    )

    assert result.ok is False
    # Receipt on disk was tampered: decision flipped to "admit" (was "refuse")
    assert result.decision == "admit"
    assert result.receipt is not None
    assert result.receipt.decision == "admit"


# -------------------------------------------------------------------
# deterministic gate_results_hash
# -------------------------------------------------------------------

from bernstein.core.quality.merge_receipt import compute_gate_results_hash


def test_gate_results_hash_deterministic():
    """Same inputs produce identical gate_results_hash."""
    h1 = compute_gate_results_hash(
        blast_radius={"score": 0.2},
        review_verdict="pass",
        required_contexts=("status/green", "build/green"),
    )
    h2 = compute_gate_results_hash(
        blast_radius={"score": 0.2},
        review_verdict="pass",
        required_contexts=("build/green", "status/green"),  # different order
    )
    assert h1 == h2
    assert h1.startswith("sha256:")


def test_gate_results_hash_differs_on_input():
    """Different inputs produce different gate_results_hash."""
    h1 = compute_gate_results_hash(
        blast_radius={"score": 0.2},
        review_verdict="pass",
        required_contexts=("status/green",),
    )
    h2 = compute_gate_results_hash(
        blast_radius={"score": 0.9},
        review_verdict="pass",
        required_contexts=("status/green",),
    )
    assert h1 != h2


# -------------------------------------------------------------------
# CLI wiring: legacy invocation vs. pick/verify subcommands (#4779)
# -------------------------------------------------------------------
#
# ``merge`` went from a single ``@click.command`` to a ``@click.group`` with
# ``pick``/``verify`` subcommands, which broke any script invoking the old
# form directly (``bernstein merge --base main --pick 2``: those options
# only existed on ``pick`` after the split). The fix keeps the legacy
# options declared on the group itself and routes both the group's
# default (no-subcommand) invocation and the ``pick`` subcommand through
# one shared function, ``_merge_pick_impl``. These tests drive all three
# surviving invocation forms through ``click.testing.CliRunner``.


def _capture_merge_pick_calls(monkeypatch):
    """Replace ``_merge_pick_impl`` and record every call's kwargs.

    Patching the one function both entry points call is what proves they
    are the same code path: unlike a ``--help`` text check, this fails the
    instant either callback stops delegating and starts running (or
    re-implementing) the body itself.
    """
    calls = []

    def _record(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr("bernstein.cli.commands.merge_cmd._merge_pick_impl", _record)
    return calls


_EXPECTED_PICK_CALL = {
    "pick_id": "2",
    "base": "release",
    "workdir": ".",
    "no_ff": True,
    "message": None,
    "dry_run": False,
    "reject_others": (),
}


def test_legacy_merge_invocation_without_subcommand_still_picks(monkeypatch):
    """``bernstein merge --base ... --pick ...`` with no subcommand still picks.

    This is the exact form #4779 broke: a script written before ``pick``
    became a subcommand calls ``merge`` directly with these options.
    """
    calls = _capture_merge_pick_calls(monkeypatch)

    result = CliRunner().invoke(merge_cmd, ["--base", "release", "--pick", "2"])

    assert result.exit_code == 0, f"{result.output}\n{result.exception!r}"
    assert calls == [_EXPECTED_PICK_CALL], calls


def test_pick_subcommand_invocation_reaches_the_same_pick_behaviour(monkeypatch):
    """``bernstein merge pick --base ... --pick ...`` reaches the identical body.

    Same options as the legacy form above, driven through the explicit
    ``pick`` subcommand instead of the bare group: the recorded call must
    be indistinguishable from it.
    """
    calls = _capture_merge_pick_calls(monkeypatch)

    result = CliRunner().invoke(merge_cmd, ["pick", "--base", "release", "--pick", "2"])

    assert result.exit_code == 0, f"{result.output}\n{result.exception!r}"
    assert calls == [_EXPECTED_PICK_CALL], calls


def test_merge_verify_invocation_still_works(populated_workdir, monkeypatch):
    """``bernstein merge verify --sha ...`` keeps working through the group.

    ``verify`` predates #4779 and takes no part in the legacy-invocation
    fix; this pins that turning ``merge`` into a group with its own
    default-path options did not disturb the pre-existing ``verify``
    subcommand.
    """
    monkeypatch.setattr(
        "bernstein.core.security.audit.load_or_create_audit_key",
        lambda *args, **kwargs: b"x" * 32,
    )

    root = populated_workdir
    head_sha = "cli_verify_sha_123"
    _emit(root, head_sha, "cli_base_456", timestamp=6000)

    result = CliRunner().invoke(merge_cmd, ["verify", "--sha", head_sha, "--workdir", str(root)])

    assert result.exit_code == 0, f"{result.output}\n{result.exception!r}"
    assert "OK" in result.output
    assert head_sha in result.output
