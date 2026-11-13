"""The delegation hop recorded when a child agent identity is minted (#5047).

``AgentIdentityStore.create_identity`` records one hop per parented mint, so
``bernstein delegation verify <run>`` reads a populated chain instead of "no
receipts".  One test per acceptance criterion, named for the property it
protects, plus the fail-closed path the issue left to the implementer.

The run manifest declares the final chain head and hop count at close (#5352).
Deleting any receipt, including the tail, must disagree with that declaration.
"""

from __future__ import annotations

import json
import shutil

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.delegation_cmd import delegation_group
from bernstein.core.identity import delegation
from bernstein.core.identity.agent_jwt import AgentIdentityStore, DelegationWriteError
from bernstein.core.identity.delegation_scope import (
    VERDICT_PASS,
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

    manifest = RunManifest(run_id=RUN, run_root_identity_id=root_identity_id)
    object.__setattr__(manifest, "manifest_hash", manifest.compute_hash())
    save_manifest(manifest, sdd_dir)


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
    assert result.chain_ok and result.authority.ok
    assert result.incomplete and not result.valid


def test_narrowing_grades_from_the_receipt_with_the_store_deleted(store, tmp_path, audit_root):
    """AC3: the scope rides on the receipt, so it grades without the store.

    The identity store directory is deleted before grading, so the test cannot
    reach it even by accident, and ``grade_chain`` is called with no
    ``scope_resolver``: the receipts are the only input.  ``task_ids`` and
    ``allowed_files`` narrowing are both proven from those receipts alone
    (#5418 made the file-scope axis gradable, via
    :func:`~bernstein.core.security.capability_tokens.glob_narrows` --
    ``src/**`` subsumes ``src/core`` over the pattern grammar, not by treating
    ``src/**`` as a path-prefix ancestor of ``src/core``).  The helper that
    decides ``path_prefixes`` is deliberately not named here:
    tests/unit/test_security_controls_are_wired.py greps bare identifiers
    across tests/, and naming it would retire an unrelated security exemption.
    """
    parent = _mint_orchestrator(store)
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
    assert verdict.verdict == VERDICT_PASS, verdict.reasons
    assert verdict.unproven_hops == 0
    rows = {row.hop_index: row for row in verdict.hops}
    assert rows[1].axes == ()


def _declare_head(audit_root, **overrides):
    from bernstein.core.config.manifest import RunManifest, save_manifest

    lines = _receipt_lines(audit_root)
    fields = {
        "run_id": RUN,
        "delegation_head_hmac": json.loads(lines[-1])["hmac"],
        "delegation_hop_count": len(lines),
        **overrides,
    }
    manifest = RunManifest.from_dict(fields)
    object.__setattr__(manifest, "delegation_seal", manifest.compute_delegation_seal(KEY))
    object.__setattr__(manifest, "manifest_hash", manifest.compute_hash())
    save_manifest(manifest, audit_root.parent)
    return manifest


@pytest.mark.parametrize("removed", [0, 1, 2])
def test_removing_any_receipt_including_the_tail_fails(store, audit_root, removed):
    parent = _mint_orchestrator(store)
    for index in range(3):
        _mint_child(store, f"child-{index}", parent, task_ids=[f"t{index}"])
    original = _receipt_lines(audit_root)
    _declare_head(audit_root, run_root_identity_id=parent.id)
    path = audit_root / "delegation" / f"{RUN}.jsonl"
    path.write_text("\n".join(line for i, line in enumerate(original) if i != removed) + "\n")

    result = CliRunner().invoke(delegation_group, ["verify", RUN, "--root", str(audit_root), "--json"])
    payload = json.loads(result.output)
    assert not payload["valid"], payload
    assert result.exit_code == 1
    if removed == 2:
        assert any("declared" in error and "mismatch" in error for error in payload["errors"])


def test_no_declared_head_is_incomplete_never_valid(store, audit_root):
    parent = _mint_orchestrator(store)
    _mint_child(store, "child", parent, task_ids=["t1"])
    result = delegation.verify_run_chain(root=audit_root, run_id=RUN, key=KEY)
    assert not result.valid
    assert result.chain_ok
    assert result.incomplete


def test_cli_incomplete_exits_three(store, audit_root):
    parent = _mint_orchestrator(store)
    _mint_child(store, "child", parent, task_ids=["t1"])
    result = CliRunner().invoke(delegation_group, ["verify", RUN, "--root", str(audit_root), "--json"])
    assert result.exit_code == 3, result.output
    payload = json.loads(result.output)
    assert payload["incomplete"] and not payload["valid"]


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"delegation_head_hmac": None},
        {"delegation_head_hmac": "é" * 64},
        {"delegation_hop_count": True},
        {"delegation_hop_count": -1},
        {"delegation_hop_count": 1.5},
        {"delegation_hop_count": 2},
        {"delegation_head_hmac": "a" * 64},
    ],
)
def test_cli_declared_head_intact_or_invalid(store, audit_root, fields):
    parent = _mint_orchestrator(store)
    _mint_child(store, "child", parent, task_ids=["t1"])
    _declare_head(audit_root, **fields)
    result = CliRunner().invoke(delegation_group, ["verify", RUN, "--root", str(audit_root), "--json"])
    payload = json.loads(result.output)
    assert payload["valid"] is (not fields), payload
    assert result.exit_code == (0 if not fields else 1)


def test_post_close_growth_does_not_replace_the_declared_head(store, audit_root):
    parent = _mint_orchestrator(store)
    _mint_child(store, "child", parent, task_ids=["t1"])
    _declare_head(audit_root)
    _mint_child(store, "later-child", parent, task_ids=["t2"])
    result = CliRunner().invoke(delegation_group, ["verify", RUN, "--root", str(audit_root), "--json"])
    payload = json.loads(result.output)
    assert not payload["valid"]
    assert result.exit_code == 1
    assert any("mismatch" in error for error in payload["errors"])


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
    # scope, and every axis on that receipt is one the comparator reads.
    _mint_child(store, "child-0", parent, task_ids=["t0"])

    _declare_head(audit_root, run_root_identity_id=parent.id)
    result = CliRunner().invoke(delegation_group, ["verify", RUN, "--root", str(audit_root)])
    assert result.exit_code == 0, result.output
    assert "1 hop(s)" in result.output


def test_a_recorded_file_scope_that_narrows_grades_pass(store, audit_root, monkeypatch):
    """A lone root recording ``allowed_files`` reads pass, structural only.

    Before #5418, any recorded ``allowed_files`` graded
    ``comparison_axis_unsupported`` regardless of what it said, because no
    primitive decided glob containment. #5418 added
    :func:`~bernstein.core.security.capability_tokens.glob_narrows`, so the
    axis is now a recognized, gradable one -- a root hop has nothing to narrow
    against and reads pass the same way every other recognized axis does.
    """
    monkeypatch.setattr(delegation, "_audit_key", lambda: KEY)
    parent = _mint_orchestrator(store)
    _mint_child(store, "child-0", parent, task_ids=["t0"], allowed_files=["src/**"])

    receipts = delegation.verify_run_chain(root=audit_root, run_id=RUN, key=KEY).receipts
    assert receipts[0].scope["allowed_files"] == ["src/**"]

    verdict = grade_chain(receipts)
    assert verdict.verdict == VERDICT_PASS
    assert verdict.unproven_hops == 0
    assert verdict.hops[0].axes == ()

    _declare_head(audit_root, run_root_identity_id=parent.id)
    result = CliRunner().invoke(delegation_group, ["verify", RUN, "--root", str(audit_root)])
    assert result.exit_code == 0, result.output

    # Note: a child minted with NO file scope under a parent that HAS one is
    # refused at mint time (agent_jwt.py's own subset check -- "an empty
    # child scope under a restricted parent is the widening direction"), so
    # that genuine widening can never reach this store's own receipts. The
    # FAIL-path coverage for that case lives in
    # tests/unit/identity/test_delegation_scope_allowed_files.py, which
    # builds receipts directly rather than through the mint path -- exactly
    # the scenario grade_chain's receipt-only grading exists for: a receipt
    # that did not come from this store's own enforcement.


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

    _declare_head(audit_root, run_root_identity_id=root_identity.id)
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
    assert "incomplete" in result.output


def test_finalizing_twice_cannot_redeclare_a_changed_chain(store, audit_root):
    from bernstein.core.config.manifest import RunManifest, load_manifest, save_manifest

    parent = _mint_orchestrator(store)
    _mint_child(store, "child", parent, task_ids=["t1"])
    manifest = RunManifest(run_id=RUN)
    object.__setattr__(manifest, "manifest_hash", manifest.compute_hash())
    save_manifest(manifest, audit_root.parent)
    ledger = delegation.DelegationLedger(audit_root, KEY)
    ledger.finalize_manifest(audit_root.parent, RUN)
    closed = load_manifest(audit_root.parent, RUN)
    ledger.finalize_manifest(audit_root.parent, RUN)
    assert closed == load_manifest(audit_root.parent, RUN)
    assert closed.manifest_hash == closed.compute_hash()
    _mint_child(store, "later", parent, task_ids=["t2"])
    with pytest.raises(ValueError, match="declared delegation head mismatch"):
        ledger.finalize_manifest(audit_root.parent, RUN)
    assert closed == load_manifest(audit_root.parent, RUN)


@pytest.mark.parametrize("damage", ["checksum", "missing", "malformed"])
def test_cli_rejects_an_unusable_final_manifest(store, audit_root, damage):
    parent = _mint_orchestrator(store)
    _mint_child(store, "child", parent, task_ids=["t1"])
    _declare_head(audit_root)
    path = audit_root.parent / "runtime" / "manifests" / f"{RUN}.json"
    if damage == "checksum":
        fields = json.loads(path.read_text())
        fields["delegation_hop_count"] = 2
        path.write_text(json.dumps(fields))
    elif damage == "missing":
        path.unlink()
    else:
        path.write_text("[]")
    result = CliRunner().invoke(delegation_group, ["verify", RUN, "--root", str(audit_root), "--json"])
    payload = json.loads(result.output)
    assert not payload["valid"]
    assert result.exit_code == (3 if damage == "missing" else 1)


def test_keyless_manifest_rewrite_cannot_hide_tail_truncation(store, audit_root):
    from dataclasses import replace

    from bernstein.core.config.manifest import load_manifest, save_manifest

    parent = _mint_orchestrator(store)
    for index in range(3):
        _mint_child(store, f"child-{index}", parent, task_ids=[f"t{index}"])
    _declare_head(audit_root, run_root_identity_id=parent.id)
    path = audit_root / "delegation" / f"{RUN}.jsonl"
    lines = path.read_text().splitlines()[:-1]
    path.write_text("\n".join(lines) + "\n")
    manifest = load_manifest(audit_root.parent, RUN)
    forged = replace(manifest, delegation_head_hmac=json.loads(lines[-1])["hmac"], delegation_hop_count=len(lines))
    forged = replace(forged, manifest_hash=forged.compute_hash())
    save_manifest(forged, audit_root.parent)
    result = delegation.verify_run(RUN, root=audit_root, key=KEY)
    assert not result.valid and not result.chain_ok
    assert any("seal" in error for error in result.errors)


def test_different_validly_keyed_tail_is_rejected(store, audit_root):
    from dataclasses import replace

    parent = _mint_orchestrator(store)
    for index in range(2):
        _mint_child(store, f"child-{index}", parent, task_ids=[f"t{index}"])
    _declare_head(audit_root, run_root_identity_id=parent.id)
    receipts = delegation.verify_run_chain(root=audit_root, run_id=RUN, key=KEY).receipts
    alternate = replace(receipts[-1], act="alternate.valid.action")
    body = alternate.body()
    alternate = replace(alternate, hmac=delegation._compute_hmac(KEY, alternate.prev_hmac, body))
    path = audit_root / "delegation" / f"{RUN}.jsonl"
    lines = path.read_text().splitlines()
    lines[-1] = json.dumps({**body, "hmac": alternate.hmac})
    path.write_text("\n".join(lines) + "\n")
    result = delegation.verify_run_chain(root=audit_root, run_id=RUN, key=KEY)
    assert not result.valid
    assert result.hops == 2
    assert any("head mismatch" in error for error in result.errors)
    assert not any("receipt tampered" in error for error in result.errors)


def test_close_refuses_to_rehash_tampered_startup_manifest(store, audit_root):
    from dataclasses import replace

    from bernstein.core.config.manifest import RunManifest, save_manifest

    parent = _mint_orchestrator(store)
    _mint_child(store, "child", parent, task_ids=["t1"])
    manifest = RunManifest(run_id=RUN)
    manifest = replace(manifest, manifest_hash=manifest.compute_hash())
    save_manifest(replace(manifest, run_root_identity_id="tampered"), audit_root.parent)
    with pytest.raises(ValueError, match="hash mismatch"):
        delegation.DelegationLedger(audit_root, KEY).finalize_manifest(audit_root.parent, RUN)
