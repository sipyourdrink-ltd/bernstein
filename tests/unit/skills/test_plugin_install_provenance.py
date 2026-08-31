"""Tests for plugin install provenance anchoring (#3540 T2).

Tests that ``bernstein skills install`` from an Agent Plugins directory
anchors each installed skill to the lineage spine + audit chain, and that
the plugin tree itself also receives a receipt.
"""

from __future__ import annotations

import hashlib
import json
import textwrap
from pathlib import Path

from scripts.gen_distribution_manifests import PLUGIN_SCHEMA_ID

from bernstein.core.security.audit import load_or_create_audit_key
from bernstein.core.security.audit_chain import AuditChainStore
from bernstein.core.skills.lifecycle import (
    InstallScope,
    install_plugin_local,
)


def _write_skill(path: Path, name: str, description: str = "Plugin skill for tests.") -> None:
    """Write a minimal valid SKILL.md with frontmatter."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        textwrap.dedent(
            f"""\
            ---
            name: {name}
            description: {description}
            ---

            # {name}

            Body content for {name}.
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )


def _write_manifest(path: Path, *, name: str, skills: str = "./skills/") -> None:
    """Write a minimal Agent Plugins v1.0.0-style plugin.json."""
    path.write_text(
        json.dumps({"$schema": PLUGIN_SCHEMA_ID, "name": name, "version": "1.0.0", "skills": skills}),
        encoding="utf-8",
    )


def _make_plugin_dir(tmp_path: Path) -> Path:
    """Create a conformant Agent Plugins directory with three valid skills."""
    root = tmp_path / "my-pack"
    skills = root / "skills"
    skills.mkdir(parents=True)
    _write_manifest(root / "plugin.json", name="my-pack")
    for name in ("alpha", "beta", "gamma"):
        _write_skill(skills / name / "SKILL.md", name)
    return root


# ---------------------------------------------------------------------------
# AC1 - install produces InstallReceipt on disk
# ---------------------------------------------------------------------------


def test_plugin_install_produces_receipts_on_disk(tmp_path: Path) -> None:
    """Each installed skill writes an InstallReceipt to .sdd/skills/receipts/."""
    root = _make_plugin_dir(tmp_path)
    workdir = tmp_path / "project"
    result = install_plugin_local(root, scope=InstallScope.PROJECT, workdir=workdir)

    assert len(result.installed) == 3
    receipts_dir = workdir / ".sdd" / "skills" / "receipts"
    assert receipts_dir.is_dir()

    # One receipt per installed skill (receipts are named by skill_hash).
    from bernstein.core.skills.provenance import read_install_receipt

    for inst_result in result.installed:
        skill_hash = inst_result.digest.digest
        receipt = read_install_receipt(workdir, skill_hash)
        assert receipt is not None, f"Missing receipt for skill {inst_result.name}"
        assert receipt.install_id.startswith("plugin:")
        assert (
            receipt.manifest_hash
            == hashlib.sha256((root / "skills" / inst_result.name / "SKILL.md").read_bytes()).hexdigest()
        )


# ---------------------------------------------------------------------------
# AC2 - receipt anchors to spine
# ---------------------------------------------------------------------------


def test_plugin_receipt_anchors_to_spine(tmp_path: Path) -> None:
    """Receipt entries are anchored in the install lineage spine."""
    root = _make_plugin_dir(tmp_path)
    workdir = tmp_path / "project"
    install_plugin_local(root, scope=InstallScope.PROJECT, workdir=workdir)

    spine = AuditChainStore(workdir / ".sdd" / "audit", key=load_or_create_audit_key())
    # Receipts were already recorded during install; verify the chain is intact.
    ok, errors = spine.verify()
    assert ok, f"Spine verification failed: {errors}"


# ---------------------------------------------------------------------------
# AC3 - audit chain event is recorded
# ---------------------------------------------------------------------------


def test_audit_chain_event_recorded_for_skill_install(tmp_path: Path) -> None:
    """The audit chain records a ``skill.install_receipt`` event per installed skill."""
    root = _make_plugin_dir(tmp_path)
    workdir = tmp_path / "project"
    install_plugin_local(root, scope=InstallScope.PROJECT, workdir=workdir)

    # Check the audit chain
    audit_dir = workdir / ".sdd" / "audit"
    chain = AuditChainStore(audit_dir, key=load_or_create_audit_key())
    events = list(chain.query(event_type="skill.install_receipt"))
    # 3 skill receipts + 1 plugin-tree receipt
    assert len(events) == 4, f"Expected 4 install receipt events, got {len(events)}"

    # Each event has the expected fields.
    for event in events:
        details = event.details
        assert "skill_hash" in details
        assert "manifest_hash" in details
        assert "install_id" in details
        assert "spine_anchor" in details


# ---------------------------------------------------------------------------
# AC4 - same-skill re-install is idempotent
# ---------------------------------------------------------------------------


def test_same_skill_reinstall_is_idempotent(tmp_path: Path) -> None:
    """Reinstalling the same skill pack succeeds silently (same-source reinstall)."""
    root = _make_plugin_dir(tmp_path)
    workdir = tmp_path / "project"

    # First install.
    result1 = install_plugin_local(root, scope=InstallScope.PROJECT, workdir=workdir)
    assert len(result1.installed) == 3
    assert result1.skipped == []

    # Reinstall same pack — same-source reinstall succeeds silently.
    result2 = install_plugin_local(root, scope=InstallScope.PROJECT, workdir=workdir)
    assert len(result2.installed) == 3
    assert result2.skipped == []

    # Lock file has one entry per skill (no duplicates).
    lock_path = workdir / "skills.lock"
    assert lock_path.is_file()
    lock_content = lock_path.read_text()
    for name in ("alpha", "beta", "gamma"):
        assert lock_content.count(f'name = "{name}"') == 1


# ---------------------------------------------------------------------------
# AC5 - plugin tree gets its own receipt
# ---------------------------------------------------------------------------


def test_plugin_tree_receipt_exists(tmp_path: Path) -> None:
    """The plugin root (containing plugin.json) gets its own content-hash receipt."""
    root = _make_plugin_dir(tmp_path)
    workdir = tmp_path / "project"
    result = install_plugin_local(root, scope=InstallScope.PROJECT, workdir=workdir)

    # The result carries spine_anchors with "(plugin-tree)" key
    assert "(plugin-tree)" in result.spine_anchors

    # Check that 4 receipts exist on disk: 3 skills + 1 plugin tree.
    receipts_dir = workdir / ".sdd" / "skills" / "receipts"
    assert receipts_dir.is_dir()
    receipt_files = list(receipts_dir.glob("*.json"))
    assert len(receipt_files) == 4


def test_plugin_tree_receipt_anchors_to_spine(tmp_path: Path) -> None:
    """The plugin-tree receipt is anchored in the install lineage spine."""
    root = _make_plugin_dir(tmp_path)
    workdir = tmp_path / "project"
    result = install_plugin_local(root, scope=InstallScope.PROJECT, workdir=workdir)

    lineage_root = workdir / ".sdd" / "lineage"
    tree_anchor = result.spine_anchors["(plugin-tree)"]

    # Re-read the anchor from the spine and verify it matches.
    from bernstein.core.lineage.spine import LineageSpine

    spine = LineageSpine(lineage_root, run_id="skills", hmac_key=load_or_create_audit_key())
    # The spine head hash should match the tree anchor.
    assert spine.head_hash() == tree_anchor
