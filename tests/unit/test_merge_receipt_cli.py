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
    MissingOracleError,
    UnverifiedShareExceededError,
    VerificationScope,
    compute_coverage_sets,
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


# -------------------------------------------------------------------
# Structured coverage sets, fail-closed, and v1 back-compat (#5398)
# -------------------------------------------------------------------


def test_unverified_set_equals_change_set_when_scope_is_empty():
    """With scopes=() and change_set=('a.py','b.py'), verified=() and
    unverified=('a.py','b.py') (sorted)."""
    verified, unverified, skipped, _ = compute_coverage_sets(
        scopes=(),
        change_set=("a.py", "b.py"),
    )
    assert verified == ()
    assert unverified == ("a.py", "b.py")
    assert skipped == ()


def test_coverage_set_hash_is_recomputable_byte_for_byte():
    """compute_coverage_sets twice with the same inputs returns the same
    hash; recomputing from the receipt's verified/unverified/skipped
    fields reproduces it."""
    scopes = (VerificationScope(oracle="lint", checked=("a.py", "b.py")),)
    change_set = ("a.py", "b.py", "c.py")
    _, _, _, hash_a = compute_coverage_sets(scopes=scopes, change_set=change_set)
    _, _, _, hash_b = compute_coverage_sets(scopes=scopes, change_set=change_set)
    assert hash_a == hash_b

    verified, unverified, skipped, _ = compute_coverage_sets(scopes=scopes, change_set=change_set)
    _, _, _, hash_recomputed = compute_coverage_sets(
        scopes=(VerificationScope(oracle="lint", checked=verified),),
        change_set=tuple(sorted(verified) + list(unverified)),
    )
    # Skipped must round-trip too for the hash to reproduce
    _, _, _, hash_recomputed = compute_coverage_sets(
        scopes=(
            VerificationScope(
                oracle="lint",
                checked=verified,
                skipped=skipped,
            ),
        ),
        change_set=change_set,
    )
    assert hash_recomputed == hash_a


def test_changing_a_scope_changes_coverage_set_hash():
    """Hash differs when one scope's checked set differs."""
    scope_a = VerificationScope(oracle="lint", checked=("a.py",))
    scope_b = VerificationScope(oracle="lint", checked=("a.py", "b.py"))
    _, _, _, hash_a = compute_coverage_sets(scopes=(scope_a,), change_set=("a.py", "b.py"))
    _, _, _, hash_b = compute_coverage_sets(scopes=(scope_b,), change_set=("a.py", "b.py"))
    assert hash_a != hash_b


def test_missing_required_oracle_fails_closed(populated_workdir):
    """emit_merge_receipt with required_oracle_kinds=('test',) and scopes
    that lack the 'test' oracle raises MissingOracleError."""
    root = populated_workdir
    hmac_key = b"x" * 32
    private_key_pem = (root / ".sdd" / "identity" / "merge-identity-key.pem").read_text(encoding="ascii")
    public_key_pem = (root / ".sdd" / "identity" / "merge-identity-public.pem").read_text(encoding="ascii")

    scopes = (VerificationScope(oracle="lint", checked=("a.py",)),)

    with pytest.raises(MissingOracleError):
        emit_merge_receipt(
            workdir=root,
            lineage_root=root / ".sdd" / "lineage",
            hmac_key=hmac_key,
            private_key_pem=private_key_pem,
            public_key_pem=public_key_pem,
            head_sha="missing_oracle_head",
            merge_base_sha="base",
            change_set=("a.py", "b.py"),
            scopes=scopes,
            required_oracle_kinds=("test",),
            timestamp=7000,
        )


def test_unverified_share_above_threshold_fails_closed(populated_workdir):
    """emit_merge_receipt with unverified_threshold=0.0 and a scope that
    does not cover one of two files raises UnverifiedShareExceededError."""
    root = populated_workdir
    hmac_key = b"x" * 32
    private_key_pem = (root / ".sdd" / "identity" / "merge-identity-key.pem").read_text(encoding="ascii")
    public_key_pem = (root / ".sdd" / "identity" / "merge-identity-public.pem").read_text(encoding="ascii")

    # scope covers only 'a.py'; change_set has 'a.py' and 'b.py'
    scopes = (VerificationScope(oracle="test", checked=("a.py",)),)
    change_set = ("a.py", "b.py")

    with pytest.raises(UnverifiedShareExceededError):
        emit_merge_receipt(
            workdir=root,
            lineage_root=root / ".sdd" / "lineage",
            hmac_key=hmac_key,
            private_key_pem=private_key_pem,
            public_key_pem=public_key_pem,
            head_sha="unverified_threshold_head",
            merge_base_sha="base",
            change_set=change_set,
            scopes=scopes,
            unverified_threshold=0.0,
            timestamp=8000,
        )


def test_receipt_signed_under_the_v1_binding_still_verifies_after_the_schema_bump(populated_workdir):
    """An archived v1 receipt stays verifiable across the v1 -> v2 bump.

    The fixture is a genuine v1 artefact: the binding dict is written out
    by hand at the v1 key set (no coverage fields), the Ed25519 signature
    is taken over *those* bytes, and *those* bytes are what the merge
    spine anchors.  Nothing here is produced by the current
    ``_binding()``, so the test cannot pass by construction -- if the
    loaded receipt re-canonicalises at v2, the signature check and the
    anchor lookup both miss.
    """
    from bernstein.core.lineage.spine import LineageSpine
    from bernstein.core.quality.merge_receipt import (
        compute_gate_results_hash,
        compute_ruleset_hash,
    )
    from bernstein.core.skills.catalog.signature import sign_payload as _sign

    root = populated_workdir
    public_key_pem = (root / ".sdd" / "identity" / "merge-identity-public.pem").read_text(encoding="ascii")
    private_key_pem = (root / ".sdd" / "identity" / "merge-identity-key.pem").read_text(encoding="ascii")
    hmac_key = b"x" * 32
    lineage_root = root / ".sdd" / "lineage"

    head_sha = "v1_backcompat_head"

    # The v1 binding, spelled out at the key set v1 actually signed.
    # Deliberately not built from MergeAdmissionReceipt._binding(): this is
    # the archived shape, and it must stay pinned even as the live schema
    # grows fields.
    v1_binding = {
        "v": 1,
        "head_sha": head_sha,
        "merge_base_sha": "v1_base",
        "required_context_ids": ["status/green"],
        "gate_results_hash": compute_gate_results_hash(
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
            required_contexts=("status/green",),
        ),
        "ruleset_hash": compute_ruleset_hash(
            required_contexts=("status/green",),
            ruleset_bytes=b"",
        ),
        "review_receipt_id": "",
        "journal_head": "",
        "decision": "admit",
        "authority": "autonomous",
        "timestamp": 9000,
    }
    v1_bytes = json.dumps(v1_binding, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")

    # Sign and anchor the v1 bytes -- not whatever the current code projects.
    v1_signature = _sign(v1_bytes, private_key_pem)
    spine = LineageSpine(lineage_root, run_id="merges", hmac_key=hmac_key)
    safe = hashlib.sha256(head_sha.encode("utf-8")).hexdigest()
    v1_anchor = spine.record(
        artifact_path=f".sdd/merges/receipts/{safe}.json",
        content=v1_bytes,
        actor="bernstein.merge_admission",
        step_id=head_sha,
        model="admission",
        timestamp=9000,
    )

    v1_row = v1_binding | {
        "signer_public_key_pem": public_key_pem,
        "signature": v1_signature,
        "journal_entry_hash": v1_anchor,
        "advisory": "",
    }
    receipt_path = root / ".sdd" / "merges" / "receipts" / f"{safe}.json"
    receipt_path.write_text(
        json.dumps(v1_row, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )

    loaded = read_merge_receipt(root, head_sha)
    assert loaded is not None
    assert loaded.decision == "admit"

    # The property under test: the loaded receipt projects back to the exact
    # bytes that were signed, so the v2 coverage keys must be absent.
    assert loaded.to_canonical_bytes() == v1_bytes
    assert b"coverage_set_hash" not in loaded.to_canonical_bytes()

    # ... and the signature and the spine anchor over those bytes both hold.
    result = verify_merge_receipt(
        workdir=root,
        lineage_root=lineage_root,
        hmac_key=hmac_key,
        head_sha=head_sha,
    )
    assert result.ok is True, result.reason
    assert result.decision == "admit"

    assert loaded.schema_version == 1
    # v1 rows carry no coverage sets, and loading must not invent any.
    assert loaded.verified == ()
    assert loaded.unverified == ()
    assert loaded.skipped == ()
    assert loaded.coverage_set_hash == ""


def test_a_v2_receipt_round_trips_through_from_dict_at_the_current_schema(populated_workdir):
    """A freshly emitted receipt reloads at the live schema version.

    Guards the other side of the version-carrying change: pinning the
    loaded version must not pin *every* receipt to v1.
    """
    from bernstein.core.quality.merge_receipt import MERGE_SCHEMA_VERSION

    root = populated_workdir
    head_sha = "v2_roundtrip_head"
    _emit(root, head_sha, "v2_roundtrip_base")

    loaded = read_merge_receipt(root, head_sha)
    assert loaded is not None
    assert loaded.schema_version == MERGE_SCHEMA_VERSION
    assert b"coverage_set_hash" in loaded.to_canonical_bytes()


def test_re_emitting_with_identical_inputs_yields_identical_binding(populated_workdir):
    """emit twice with the same inputs (deterministic timestamp) yields
    to_canonical_bytes() that match."""
    root = populated_workdir
    hmac_key = b"x" * 32
    private_key_pem = (root / ".sdd" / "identity" / "merge-identity-key.pem").read_text(encoding="ascii")
    public_key_pem = (root / ".sdd" / "identity" / "merge-identity-public.pem").read_text(encoding="ascii")

    kwargs = dict(
        workdir=root,
        lineage_root=root / ".sdd" / "lineage",
        hmac_key=hmac_key,
        private_key_pem=private_key_pem,
        public_key_pem=public_key_pem,
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
        timestamp=10000,
    )
    receipt1 = emit_merge_receipt(head_sha="re_emit_a", merge_base_sha="re_emit_base", **kwargs)
    receipt2 = emit_merge_receipt(head_sha="re_emit_a", merge_base_sha="re_emit_base", **kwargs)

    assert receipt1.to_canonical_bytes() == receipt2.to_canonical_bytes()
    assert receipt1.coverage_set_hash == receipt2.coverage_set_hash


def test_coverage_set_hash_is_signed(populated_workdir):
    """Edit a single character of the stored receipt's 'verified' list
    and re-verify: signature must fail."""
    root = populated_workdir
    hmac_key = b"x" * 32
    head_sha = "coverage_signed_head"
    private_key_pem = (root / ".sdd" / "identity" / "merge-identity-key.pem").read_text(encoding="ascii")
    public_key_pem = (root / ".sdd" / "identity" / "merge-identity-public.pem").read_text(encoding="ascii")

    # Cover both files so the unverified share stays under the default 0.0 threshold
    emit_merge_receipt(
        workdir=root,
        lineage_root=root / ".sdd" / "lineage",
        hmac_key=hmac_key,
        private_key_pem=private_key_pem,
        public_key_pem=public_key_pem,
        head_sha=head_sha,
        merge_base_sha="coverage_signed_base",
        change_set=("a.py", "b.py"),
        scopes=(VerificationScope(oracle="test", checked=("a.py", "b.py")),),
        decision="admit",
        timestamp=11000,
    )

    # Tamper: edit the 'verified' list in the stored JSON
    safe = hashlib.sha256(head_sha.encode("utf-8")).hexdigest()
    receipt_path = root / ".sdd" / "merges" / "receipts" / f"{safe}.json"
    data = json.loads(receipt_path.read_text(encoding="utf-8"))
    data["verified"][0] = data["verified"][0] + "X"
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
    # Signature must fail: reason mentions signature
    assert "signature" in result.reason.lower()
