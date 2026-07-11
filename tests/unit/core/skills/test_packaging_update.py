"""Unit tests for the receipt-backed update path (issue #2369, tail).

The base install path (#2412) anchors a content-addressed install receipt.
An update is not a fresh install: it supersedes a *previously attested*
installed tree. The update artifact is itself a content-addressed record
that binds ``prior_skill_hash -> skill_hash`` into the ``skills`` lineage
spine plus the HMAC audit chain, so the supersession is chain-verifiable
back to the root install.

Covers:

* ``update_packaged_install`` refuses a tree that was never attested (an
  update must chain onto an anchored prior install).
* An update rewrites the tree, anchors an :class:`UpdateReceipt`, records a
  ``plugin.update_receipt`` audit event, and is byte-deterministic.
* An idempotent update (source already current) writes no new receipt.
* ``verify_packaged_install`` passes on an updated tree (the new content
  address resolves to the update receipt) and detects tamper afterwards.
* ``resolve_install_chain`` walks newest -> root across update receipts.
* ``discover_installs`` enumerates host/scope destinations for the status UX.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bernstein.core.lineage.spine import LineageSpine
from bernstein.core.security.audit_chain import (
    EVENT_PLUGIN_UPDATE_RECEIPT,
    AuditChainStore,
    record_plugin_update_receipt,
)
from bernstein.core.skills.packaging import (
    PACKAGED_SKILL_NAME,
    PackagedInstallError,
    discover_installs,
    install_packaged_skill,
    resolve_install_chain,
    tree_content_hash,
    update_packaged_install,
    verify_packaged_install,
)
from bernstein.core.skills.provenance import (
    INSTALL_RUN_ID,
    read_update_receipt,
)

if TYPE_CHECKING:
    from pathlib import Path

_KEY = b"0" * 32


def _write_tree(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _skill_v1(root: Path) -> Path:
    skill = root / "src-v1"
    _write_tree(
        skill,
        {
            "SKILL.md": "---\nname: bernstein-run\ndescription: v1\n---\nbody v1\n",
            "references/examples.md": "example v1\n",
        },
    )
    return skill


def _skill_v2(root: Path) -> Path:
    skill = root / "src-v2"
    _write_tree(
        skill,
        {
            "SKILL.md": "---\nname: bernstein-run\ndescription: v2\n---\nbody v2 updated\n",
            "references/examples.md": "example v2\n",
        },
    )
    return skill


def _install_v1(tmp_path: Path) -> tuple[Path, Path]:
    workdir = tmp_path / "proj"
    workdir.mkdir()
    dest = tmp_path / "host" / PACKAGED_SKILL_NAME
    install_packaged_skill(
        workdir=workdir,
        dest=dest,
        source=_skill_v1(tmp_path),
        hmac_key=_KEY,
        install_id="agent-plugin-claude-project",
        timestamp=100,
        host="claude",
        scope="project",
    )
    return workdir, dest


# ---------------------------------------------------------------------------
# Update guardrails
# ---------------------------------------------------------------------------


def test_update_refuses_unattested_tree(tmp_path: Path) -> None:
    """A tree with no prior receipt cannot be updated; use install instead."""
    workdir = tmp_path / "proj"
    workdir.mkdir()
    dest = tmp_path / "host" / PACKAGED_SKILL_NAME
    _write_tree(dest, {"SKILL.md": "handmade\n"})

    with pytest.raises(PackagedInstallError, match="attested"):
        update_packaged_install(
            workdir=workdir,
            dest=dest,
            source=_skill_v2(tmp_path),
            hmac_key=_KEY,
            install_id="upd",
            timestamp=200,
            host="claude",
            scope="project",
        )


def test_update_missing_dest_raises(tmp_path: Path) -> None:
    workdir = tmp_path / "proj"
    workdir.mkdir()
    with pytest.raises(PackagedInstallError):
        update_packaged_install(
            workdir=workdir,
            dest=tmp_path / "absent" / PACKAGED_SKILL_NAME,
            source=_skill_v2(tmp_path),
            hmac_key=_KEY,
            install_id="upd",
            timestamp=200,
            host="claude",
            scope="project",
        )


# ---------------------------------------------------------------------------
# Update happy path
# ---------------------------------------------------------------------------


def test_update_rewrites_tree_and_anchors_update_receipt(tmp_path: Path) -> None:
    workdir, dest = _install_v1(tmp_path)
    prior_hash = tree_content_hash(dest)

    outcome = update_packaged_install(
        workdir=workdir,
        dest=dest,
        source=_skill_v2(tmp_path),
        hmac_key=_KEY,
        install_id="agent-plugin-claude-project",
        timestamp=200,
        host="claude",
        scope="project",
    )

    assert outcome.changed
    assert outcome.prior_skill_hash == prior_hash
    assert outcome.skill_hash == tree_content_hash(dest)
    assert outcome.skill_hash != prior_hash
    assert "body v2 updated" in (dest / "SKILL.md").read_text(encoding="utf-8")

    receipt = read_update_receipt(workdir, outcome.skill_hash)
    assert receipt is not None
    assert receipt.prior_skill_hash == prior_hash
    assert receipt.skill_hash == outcome.skill_hash
    assert receipt.manifest_hash == outcome.manifest_hash

    spine = LineageSpine(workdir / ".sdd" / "lineage", run_id=INSTALL_RUN_ID, hmac_key=_KEY)
    assert spine.verify().ok
    assert spine.head_hash() == outcome.spine_anchor

    chain = AuditChainStore(workdir / ".sdd" / "audit", key=_KEY)
    ok, errors = chain.verify()
    assert ok, errors
    events = chain.query(event_type=EVENT_PLUGIN_UPDATE_RECEIPT)
    assert len(events) == 1
    assert events[0].details["prior_skill_hash"] == prior_hash
    assert events[0].details["skill_hash"] == outcome.skill_hash


def test_update_is_idempotent_when_source_unchanged(tmp_path: Path) -> None:
    workdir, dest = _install_v1(tmp_path)
    outcome = update_packaged_install(
        workdir=workdir,
        dest=dest,
        source=_skill_v1(tmp_path),
        hmac_key=_KEY,
        install_id="agent-plugin-claude-project",
        timestamp=200,
        host="claude",
        scope="project",
    )
    assert not outcome.changed
    # No update receipt is written for a no-op.
    assert read_update_receipt(workdir, outcome.skill_hash) is None


def test_update_is_deterministic_across_fresh_workdirs(tmp_path: Path) -> None:
    anchors: list[str] = []
    receipts: list[bytes] = []
    for name in ("one", "two"):
        base = tmp_path / name
        base.mkdir()
        workdir = base / "proj"
        workdir.mkdir()
        dest = base / "host" / PACKAGED_SKILL_NAME
        install_packaged_skill(
            workdir=workdir,
            dest=dest,
            source=_skill_v1(base),
            hmac_key=_KEY,
            install_id="agent-plugin-claude-project",
            timestamp=100,
            host="claude",
            scope="project",
        )
        outcome = update_packaged_install(
            workdir=workdir,
            dest=dest,
            source=_skill_v2(base),
            hmac_key=_KEY,
            install_id="agent-plugin-claude-project",
            timestamp=200,
            host="claude",
            scope="project",
        )
        anchors.append(outcome.spine_anchor)
        receipt = read_update_receipt(workdir, outcome.skill_hash)
        assert receipt is not None
        receipts.append(receipt.to_canonical_bytes())
    assert anchors[0] == anchors[1]
    assert receipts[0] == receipts[1]


# ---------------------------------------------------------------------------
# Verify + chain resolution
# ---------------------------------------------------------------------------


def test_verify_passes_on_updated_tree(tmp_path: Path) -> None:
    workdir, dest = _install_v1(tmp_path)
    update_packaged_install(
        workdir=workdir,
        dest=dest,
        source=_skill_v2(tmp_path),
        hmac_key=_KEY,
        install_id="agent-plugin-claude-project",
        timestamp=200,
        host="claude",
        scope="project",
    )
    result = verify_packaged_install(workdir=workdir, dest=dest, hmac_key=_KEY)
    assert result.ok, result.reason


def test_verify_detects_tamper_after_update(tmp_path: Path) -> None:
    workdir, dest = _install_v1(tmp_path)
    update_packaged_install(
        workdir=workdir,
        dest=dest,
        source=_skill_v2(tmp_path),
        hmac_key=_KEY,
        install_id="agent-plugin-claude-project",
        timestamp=200,
        host="claude",
        scope="project",
    )
    (dest / "SKILL.md").write_text("tampered after update\n", encoding="utf-8")
    result = verify_packaged_install(workdir=workdir, dest=dest, hmac_key=_KEY)
    assert not result.ok


def test_resolve_install_chain_walks_newest_to_root(tmp_path: Path) -> None:
    workdir, dest = _install_v1(tmp_path)
    root_hash = tree_content_hash(dest)
    up1 = update_packaged_install(
        workdir=workdir,
        dest=dest,
        source=_skill_v2(tmp_path),
        hmac_key=_KEY,
        install_id="agent-plugin-claude-project",
        timestamp=200,
        host="claude",
        scope="project",
    )
    v3 = tmp_path / "src-v3"
    _write_tree(v3, {"SKILL.md": "---\nname: bernstein-run\n---\nv3\n"})
    up2 = update_packaged_install(
        workdir=workdir,
        dest=dest,
        source=v3,
        hmac_key=_KEY,
        install_id="agent-plugin-claude-project",
        timestamp=300,
        host="claude",
        scope="project",
    )

    chain = resolve_install_chain(workdir=workdir, skill_hash=up2.skill_hash)
    hashes = [link.skill_hash for link in chain]
    assert hashes == [up2.skill_hash, up1.skill_hash, root_hash]
    # The oldest link is the root install (no prior); newer links carry a prior.
    assert chain[0].prior_skill_hash == up1.skill_hash
    assert chain[-1].prior_skill_hash is None
    assert chain[-1].is_install


def test_resolve_install_chain_single_install_has_one_link(tmp_path: Path) -> None:
    workdir, dest = _install_v1(tmp_path)
    root_hash = tree_content_hash(dest)
    chain = resolve_install_chain(workdir=workdir, skill_hash=root_hash)
    assert len(chain) == 1
    assert chain[0].skill_hash == root_hash
    assert chain[0].is_install


# ---------------------------------------------------------------------------
# Discovery for the status UX
# ---------------------------------------------------------------------------


def test_discover_installs_reports_existing_destinations(tmp_path: Path) -> None:
    workdir = tmp_path / "proj"
    workdir.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    install_packaged_skill(
        workdir=workdir,
        dest=workdir / ".claude" / "skills" / PACKAGED_SKILL_NAME,
        source=_skill_v1(tmp_path),
        hmac_key=_KEY,
        install_id="agent-plugin-claude-project",
        timestamp=100,
        host="claude",
        scope="project",
    )
    found = discover_installs(workdir=workdir, home=home)
    existing = [d for d in found if d.exists]
    assert any(d.host == "claude" and d.scope == "project" for d in existing)
    # Every supported host/scope pair is enumerated whether present or not.
    assert any(d.host == "codex" and not d.exists for d in found)


# ---------------------------------------------------------------------------
# Audit event (direct)
# ---------------------------------------------------------------------------


def test_record_plugin_update_receipt_chains(tmp_path: Path) -> None:
    chain = AuditChainStore(tmp_path, key=_KEY)
    ev = record_plugin_update_receipt(
        chain=chain,
        prior_skill_hash="sha256:" + "a" * 64,
        skill_hash="sha256:" + "b" * 64,
        manifest_hash="c" * 64,
        install_id="i1",
        spine_anchor="sha256:" + "d" * 64,
        host="claude",
        scope="project",
        dest="/tmp/x",
    )
    assert ev.event_type == EVENT_PLUGIN_UPDATE_RECEIPT
    assert ev.details["prior_skill_hash"] == "sha256:" + "a" * 64
    assert ev.details["prev_chain_digest"]
    ok, errors = chain.verify()
    assert ok, errors
