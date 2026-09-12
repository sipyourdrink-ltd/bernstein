"""The delegation hop recorded when a child agent identity is minted (#5047).

``AgentIdentityStore.create_identity`` records one hop per parented mint, so
``bernstein delegation verify <run>`` reads a populated chain instead of "no
receipts".  One test per acceptance criterion, named for the property it
protects, plus the fail-closed path the issue left to the implementer.

Two tests record limits of the existing model rather than criteria met.
``test_removing_the_tail_receipt_yields_valid_true_and_one_fewer_hop`` shows the
one case AC4 cannot reach without a receipt-format change, and
``test_multi_sibling_grading_limitation`` is deliberately attached to no
criterion number at all: AC6 asks only that the CLI exit 0 and print the hop
count, and that is covered on its own.
"""

from __future__ import annotations

import shutil

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.delegation_cmd import delegation_group
from bernstein.core.identity import delegation
from bernstein.core.identity.agent_jwt import AgentIdentityStore, DelegationWriteError
from bernstein.core.identity.delegation_scope import (
    REASON_COMPARISON_AXIS_UNSUPPORTED,
    VERDICT_UNPROVEN,
    grade_chain,
)

KEY = b"k" * 32
RUN = "run-5047"


@pytest.fixture
def audit_root(tmp_path):
    """The delegation root the store writes to (sibling of its own ``auth`` dir)."""
    return tmp_path / "audit"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(delegation, "_audit_key", lambda: KEY)
    return AgentIdentityStore(tmp_path / "auth")


def _receipt_lines(audit_root) -> list[str]:
    path = audit_root / "delegation" / f"{RUN}.jsonl"
    if not path.is_file():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_manifest(sdd_dir, root_identity_id: str) -> None:
    """Write the run manifest declaring ``root_identity_id`` as the run root.

    The real orchestrator writes this at run start via ``save_manifest``; the
    tests use the same public builder so the field name and the on-disk location
    cannot drift apart from production.
    """
    from bernstein.core.config.manifest import RunManifest, save_manifest

    save_manifest(RunManifest(run_id=RUN, run_root_identity_id=root_identity_id), sdd_dir)


def _mint_orchestrator(store):
    identity, _ = store.create_identity("orch-1", "manager", metadata={"run_id": RUN})
    return identity


def _mint_child(store, name, parent, *, task_ids, allowed_files=None):
    identity, _ = store.create_identity(
        name,
        "backend",
        parent_identity_id=parent.id,
        task_ids=task_ids,
        allowed_files=allowed_files,
        metadata={"run_id": RUN},
    )
    return identity


def test_parented_mint_writes_exactly_one_receipt(store, audit_root):
    """AC1: one named parent, one receipt in the run's delegation root."""
    parent = _mint_orchestrator(store)
    _mint_child(store, "child-1", parent, task_ids=["t1"], allowed_files=["src/**"])
    assert len(_receipt_lines(audit_root)) == 1


def test_chain_over_several_spawned_agents_passes_with_one_hop_per_mint(store, audit_root):
    """AC2: several spawned agents verify, one hop each, no sibling read as a child."""
    parent = _mint_orchestrator(store)
    for index in range(3):
        _mint_child(store, f"child-{index}", parent, task_ids=[f"t{index}"], allowed_files=["src/**"])
    result = delegation.verify_run_chain(root=audit_root, run_id=RUN, key=KEY)
    assert result.hops == 3
    assert result.valid, result.errors + [str(v) for v in result.authority.violations]


def test_narrowing_grades_from_the_receipt_with_the_store_deleted(store, tmp_path, audit_root):
    """AC3: the scope rides on the receipt, so it grades without the store.

    The identity store directory is deleted before grading, so the test cannot
    reach it even by accident, and ``grade_chain`` is called with no
    ``scope_resolver``: the receipts are the only input.  ``task_ids`` narrowing
    is proven from those receipts alone; the file scope is recorded and not
    graded, which is what the chain verdict says.
    """
    parent = _mint_orchestrator(store)
    # ``allowed_files`` rides on the receipt verbatim and is deliberately not
    # graded: it is a glob field, and a glob is not a path prefix, so it cannot
    # be decided by the ancestry primitive ``path_prefixes`` uses.  The hop
    # grades ``unproven`` on that axis with a named reason rather than reporting
    # a narrowing nothing checked (#5351; the grading primitive is #5418).  The
    # helper that decides ``path_prefixes`` is deliberately not named here:
    # tests/unit/test_security_controls_are_wired.py greps bare identifiers
    # across tests/, and naming it would retire an unrelated security exemption.
    # The parent scope is the tree ``src/**`` because the mint-time check reads
    # these patterns the way the merge gate does, where ``src`` admits the path
    # ``src`` and nothing under it.
    child = _mint_child(store, "child-1", parent, task_ids=["t1", "t2"], allowed_files=["src/**"])
    _mint_child(store, "grand-1", child, task_ids=["t1"], allowed_files=["src/core"])

    shutil.rmtree(tmp_path / "auth")
    assert not (tmp_path / "auth").exists()

    receipts = delegation.verify_run_chain(root=audit_root, run_id=RUN, key=KEY).receipts
    assert len(receipts) == 2
    assert receipts[0].scope is not None
    assert receipts[0].scope["task_ids"] == ["t1", "t2"]
    assert receipts[0].scope["allowed_files"] == ["src/**"]
    assert receipts[0].scope["path_prefixes"] is None
    assert receipts[1].scope["task_ids"] == ["t1"]
    assert receipts[1].scope["allowed_files"] == ["src/core"]
    assert receipts[1].scope["path_prefixes"] is None

    verdict = grade_chain(receipts)
    assert verdict.verdict == VERDICT_UNPROVEN, verdict.reasons
    assert verdict.unproven_hops == 2
    rows = {row.hop_index: row for row in verdict.hops}
    assert rows[1].axes == ("allowed_files",)
    assert REASON_COMPARISON_AXIS_UNSUPPORTED in rows[1].reasons


def test_removing_the_tail_receipt_yields_valid_true_and_one_fewer_hop(store, audit_root):
    """AC4 holds for every receipt except the tail, which this model cannot detect (#5352).

    The loop removes each receipt in turn.  Removing the first or any interior
    receipt breaks ``prev_hmac`` linkage and verification fails, which is AC4.
    Removing the TAIL yields ``valid=True`` with ``hops == n - 1``: the shorter
    chain is internally consistent, because no receipt records how many hops the
    run should have had, so an end truncation is indistinguishable from a run
    that simply delegated one fewer time.

    This is a demonstrated limit of the existing receipt model, not a defect
    introduced here and not something to engineer around.  Detecting it needs a
    hop count, a terminator, a sidecar or some other completeness state on the
    receipt, and #5047 excludes changing the receipt format.  The assertion is
    written the way the behaviour actually is, so a future format change that
    closes the gap will trip this test rather than pass unnoticed.
    """
    parent = _mint_orchestrator(store)
    for index in range(3):
        _mint_child(store, f"child-{index}", parent, task_ids=[f"t{index}"], allowed_files=["src/**"])
    original = _receipt_lines(audit_root)
    assert len(original) == 3
    path = audit_root / "delegation" / f"{RUN}.jsonl"

    for removed in range(len(original)):
        kept = [line for index, line in enumerate(original) if index != removed]
        path.write_text("\n".join(kept) + "\n", encoding="utf-8")
        result = delegation.verify_run_chain(root=audit_root, run_id=RUN, key=KEY)
        if removed == len(original) - 1:
            assert result.valid, "tail truncation is expected to remain undetectable"
            assert result.hops == len(original) - 1
        else:
            assert not result.valid, f"removing receipt {removed} left the chain passing"


def test_parentless_mint_writes_no_receipt_and_leaves_no_broken_chain(store, audit_root):
    """AC5: no parent, no receipt; an empty chain is absent, never broken."""
    store.create_identity("orch-1", "manager", metadata={"run_id": RUN})
    store.create_identity("orch-2", "manager", metadata={"run_id": RUN})
    assert _receipt_lines(audit_root) == []

    result = delegation.verify_run_chain(root=audit_root, run_id=RUN, key=KEY)
    assert result.hops == 0
    assert result.errors == ["no delegation receipts for run"]
    assert not any("linkage" in err or "HMAC" in err for err in result.errors)


def test_failed_hop_write_aborts_the_mint_and_writes_no_partial_receipt(store, tmp_path, audit_root, monkeypatch):
    """The fail-closed path, including the residue it leaves.

    The ledger's error is re-raised as :class:`DelegationWriteError` so the
    spawner can single this failure out from every other identity failure and
    refuse the spawn; the original is chained as ``__cause__``.

    A ledger that raises takes the mint down with it: ``create_identity``
    propagates and returns nothing, so the caller never receives the raw token
    and the credential cannot be presented.  There is no rollback, and this
    pins what survives: the identity JSON is already written, no ``created``
    audit event is appended, and no receipt exists.  #5047 says nothing about
    rollback and the repository has no transaction convention to borrow, so the
    residue is recorded rather than swept up.
    """
    parent = _mint_orchestrator(store)

    def _boom(**_kwargs):
        msg = "ledger unavailable"
        raise OSError(msg)

    monkeypatch.setattr(delegation, "record_delegation_hop", _boom)

    with pytest.raises(DelegationWriteError) as caught:
        _mint_child(store, "child-1", parent, task_ids=["t1"], allowed_files=["src"])
    # The ledger's own error is chained, so nothing about the cause is lost.
    assert isinstance(caught.value.__cause__, OSError)
    assert "ledger unavailable" in str(caught.value.__cause__)

    # The property the issue asks for: nothing partial reaches the chain.
    assert _receipt_lines(audit_root) == []

    # The residue, asserted so it cannot change unnoticed.
    assert (tmp_path / "auth" / "agent_identities" / "child-1.json").is_file()
    audit_rows = (tmp_path / "auth" / "agent_identity_audit.jsonl").read_text(encoding="utf-8")
    assert "child-1" not in audit_rows


def test_cli_verify_exits_zero_and_prints_the_hop_count(store, audit_root, monkeypatch):
    """AC6: `bernstein delegation verify <run>` exits 0 and prints the hop count."""
    monkeypatch.setattr(delegation, "_audit_key", lambda: KEY)
    parent = _mint_orchestrator(store)
    # The production shape: the spawner mints with ``task_ids`` and no file
    # scope, and every axis on that receipt is one the comparator reads.  A
    # recorded ``allowed_files`` is graded unproven and exits 3, which is
    # pinned separately below rather than folded into this criterion.
    _mint_child(store, "child-0", parent, task_ids=["t0"])

    result = CliRunner().invoke(delegation_group, ["verify", RUN, "--root", str(audit_root)])
    assert result.exit_code == 0, result.output
    assert "1 hop(s)" in result.output


def test_a_recorded_file_scope_is_unproven_and_the_cli_exits_three(store, audit_root, monkeypatch):
    """A recorded ``allowed_files`` is not graded, so the chain is unproven.

    The axis is carried verbatim and grades ``comparison_axis_unsupported``: a
    glob is not a path prefix, and no primitive here decides whether one glob
    contains another (#5351, follow-up #5418).  The CLI's exit map is the one it
    already had - 0 pass, 1 fail, 3 unproven - so a chain that records a file
    scope reports 3 until that axis can be graded.
    """
    monkeypatch.setattr(delegation, "_audit_key", lambda: KEY)
    parent = _mint_orchestrator(store)
    _mint_child(store, "child-0", parent, task_ids=["t0"], allowed_files=["src/**"])

    receipts = delegation.verify_run_chain(root=audit_root, run_id=RUN, key=KEY).receipts
    assert receipts[0].scope["allowed_files"] == ["src/**"]

    verdict = grade_chain(receipts)
    assert verdict.verdict == VERDICT_UNPROVEN
    assert verdict.unproven_hops == 1
    assert verdict.hops[0].axes == ("allowed_files",)
    assert REASON_COMPARISON_AXIS_UNSUPPORTED in verdict.hops[0].reasons

    result = CliRunner().invoke(delegation_group, ["verify", RUN, "--root", str(audit_root)])
    assert result.exit_code == 3, result.output
    assert "comparison_axis_unsupported" in result.output


def test_siblings_grade_when_the_manifest_declares_the_run_root(store, tmp_path, audit_root, monkeypatch):
    """Two children of one run root now BOTH grade, anchored from the manifest.

    Last pass this was ``test_multi_sibling_grading_limitation`` and asserted
    exit 3: root status is positional, so the second sibling read as
    ``root_claimed_mid_chain`` -> unproven. It is no longer a limitation. The run
    manifest records the identity the orchestrator minted as the run root, and
    ``delegation verify`` reads that name from the manifest, so a hop issued by
    the declared root may be a root without being first.

    The name comes from OUTSIDE the receipts, which is why this does not reopen
    the evasion the positional rule exists to stop: a receipt still cannot
    promote itself by writing its own issuer field.
    """
    monkeypatch.setattr(delegation, "_audit_key", lambda: KEY)
    root_identity, _ = store.create_identity("run-root-1", "manager", metadata={"run_id": RUN})
    _write_manifest(tmp_path, root_identity.id)

    for index in range(2):
        store.create_identity(
            f"child-{index}",
            "backend",
            parent_identity_id=root_identity.id,
            task_ids=[f"t{index}"],
            metadata={"run_id": RUN},
        )

    result = CliRunner().invoke(delegation_group, ["verify", RUN, "--root", str(audit_root)])
    assert result.exit_code == 0, result.output
    assert "2 hop(s)" in result.output
    assert "root_claimed_mid_chain" not in result.output


def test_without_a_manifest_the_positional_root_rule_is_unchanged(store, audit_root, monkeypatch):
    """No manifest, no declared root: the second sibling is unproven, as before.

    The anchor is additive. A run whose root is not declared keeps exactly the
    grading it had, which is what stops this from being a general loosening of
    the positional rule.
    """
    monkeypatch.setattr(delegation, "_audit_key", lambda: KEY)
    root_identity, _ = store.create_identity("run-root-1", "manager", metadata={"run_id": RUN})
    for index in range(2):
        store.create_identity(
            f"child-{index}",
            "backend",
            parent_identity_id=root_identity.id,
            task_ids=[f"t{index}"],
            allowed_files=["src"],
            metadata={"run_id": RUN},
        )
    result = CliRunner().invoke(delegation_group, ["verify", RUN, "--root", str(audit_root)])
    assert result.exit_code == 3, result.output
    assert "root_claimed_mid_chain" in result.output
