"""Unit tests for the packaged agent-skill install path (issue #2369).

Covers the receipt-backed install acceptance criterion:

* The bundled ``bernstein-run`` skill resolves from the package tree.
* ``tree_content_hash`` is a deterministic content address over a
  directory tree (location-independent, byte-sensitive, name-sensitive).
* ``install_packaged_skill`` copies the skill into a host directory and
  anchors an install receipt in the ``skills`` lineage spine plus the
  HMAC audit chain.
* ``verify_packaged_install`` recomputes the installed tree's content
  address and receipt; tampering with the installed files or the spine
  is detected.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bernstein.core.lineage.spine import LineageSpine
from bernstein.core.security.audit_chain import (
    EVENT_PLUGIN_INSTALL_RECEIPT,
    AuditChainStore,
    record_plugin_install_receipt,
)
from bernstein.core.skills.packaging import (
    PACKAGED_SKILL_NAME,
    PackagedInstallError,
    host_skill_parent,
    install_packaged_skill,
    manifest_hash_for,
    packaged_skill_dir,
    tree_content_hash,
    verify_packaged_install,
)
from bernstein.core.skills.provenance import INSTALL_RUN_ID, read_install_receipt

if TYPE_CHECKING:
    from pathlib import Path

_KEY = b"0" * 32


def _write_tree(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _skill_fixture(root: Path) -> Path:
    skill = root / "src-skill"
    _write_tree(
        skill,
        {
            "SKILL.md": "---\nname: bernstein-run\ndescription: run a verified goal\n---\nbody\n",
            "references/examples.md": "example\n",
        },
    )
    return skill


# ---------------------------------------------------------------------------
# Content address
# ---------------------------------------------------------------------------


def test_tree_content_hash_is_location_independent(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "elsewhere" / "b"
    files = {"SKILL.md": "content\n", "references/x.md": "ref\n"}
    _write_tree(a, files)
    _write_tree(b, files)
    assert tree_content_hash(a) == tree_content_hash(b)
    assert tree_content_hash(a).startswith("sha256:")


def test_tree_content_hash_detects_byte_and_name_changes(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    c = tmp_path / "c"
    _write_tree(a, {"SKILL.md": "content\n"})
    _write_tree(b, {"SKILL.md": "Content\n"})
    _write_tree(c, {"SKILL2.md": "content\n"})
    assert tree_content_hash(a) != tree_content_hash(b)
    assert tree_content_hash(a) != tree_content_hash(c)


def test_manifest_hash_prefers_skill_md(tmp_path: Path) -> None:
    skill = _skill_fixture(tmp_path)
    rel, digest = manifest_hash_for(skill)
    assert rel == "SKILL.md"
    assert len(digest) == 64


def test_tree_content_hash_rejects_symlinked_root(tmp_path: Path) -> None:
    """A symlinked tree root is refused: following it would hash bytes outside
    the requested path (security, issue #2642)."""
    real = tmp_path / "real"
    _write_tree(real, {"SKILL.md": "content\n"})
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(PackagedInstallError):
        tree_content_hash(link)


# ---------------------------------------------------------------------------
# Bundled asset resolution
# ---------------------------------------------------------------------------


def test_packaged_skill_dir_resolves_and_has_manifest() -> None:
    skill = packaged_skill_dir()
    assert skill.is_dir()
    assert skill.name == PACKAGED_SKILL_NAME
    body = (skill / "SKILL.md").read_text(encoding="utf-8")
    assert body.startswith("---")
    assert "name: bernstein-run" in body


def test_host_skill_parent_project_and_user_scopes(tmp_path: Path) -> None:
    project = host_skill_parent("claude", "project", workdir=tmp_path, home=tmp_path / "home")
    user = host_skill_parent("claude", "user", workdir=tmp_path, home=tmp_path / "home")
    assert project == tmp_path / ".claude" / "skills"
    assert user == tmp_path / "home" / ".claude" / "skills"
    with pytest.raises(PackagedInstallError):
        host_skill_parent("unknown-host", "project", workdir=tmp_path, home=tmp_path / "home")


# ---------------------------------------------------------------------------
# Install: copy + receipt + audit chain
# ---------------------------------------------------------------------------


def test_install_copies_and_anchors_receipt(tmp_path: Path) -> None:
    workdir = tmp_path / "proj"
    workdir.mkdir()
    source = _skill_fixture(tmp_path)
    dest = tmp_path / "host" / PACKAGED_SKILL_NAME

    outcome = install_packaged_skill(
        workdir=workdir,
        dest=dest,
        source=source,
        hmac_key=_KEY,
        install_id="agent-plugin-test",
        timestamp=42,
    )

    assert outcome.copied
    assert (dest / "SKILL.md").is_file()
    assert (dest / "references" / "examples.md").is_file()
    assert outcome.skill_hash == tree_content_hash(dest)
    assert outcome.spine_anchor.startswith("sha256:")

    receipt = read_install_receipt(workdir, outcome.skill_hash)
    assert receipt is not None
    assert receipt.install_id == "agent-plugin-test"
    assert receipt.manifest_hash == outcome.manifest_hash

    spine = LineageSpine(workdir / ".sdd" / "lineage", run_id=INSTALL_RUN_ID, hmac_key=_KEY)
    assert spine.verify().ok
    assert spine.head_hash() == outcome.spine_anchor

    chain = AuditChainStore(workdir / ".sdd" / "audit", key=_KEY)
    ok, errors = chain.verify()
    assert ok, errors
    events = chain.query(event_type=EVENT_PLUGIN_INSTALL_RECEIPT)
    assert len(events) == 1
    assert events[0].details["spine_anchor"] == outcome.spine_anchor
    assert events[0].details["skill_hash"] == outcome.skill_hash


def test_install_is_deterministic_across_fresh_workdirs(tmp_path: Path) -> None:
    source = _skill_fixture(tmp_path)
    anchors: list[str] = []
    receipts: list[bytes] = []
    for name in ("one", "two"):
        workdir = tmp_path / name
        workdir.mkdir()
        outcome = install_packaged_skill(
            workdir=workdir,
            dest=tmp_path / f"host-{name}" / PACKAGED_SKILL_NAME,
            source=source,
            hmac_key=_KEY,
            install_id="agent-plugin-test",
            timestamp=42,
        )
        anchors.append(outcome.spine_anchor)
        receipt = read_install_receipt(workdir, outcome.skill_hash)
        assert receipt is not None
        receipts.append(receipt.to_canonical_bytes())
    assert anchors[0] == anchors[1]
    assert receipts[0] == receipts[1]


def test_install_refuses_divergent_dest_without_force(tmp_path: Path) -> None:
    workdir = tmp_path / "proj"
    workdir.mkdir()
    source = _skill_fixture(tmp_path)
    dest = tmp_path / "host" / PACKAGED_SKILL_NAME
    _write_tree(dest, {"SKILL.md": "something else\n"})

    with pytest.raises(PackagedInstallError):
        install_packaged_skill(
            workdir=workdir,
            dest=dest,
            source=source,
            hmac_key=_KEY,
            install_id="i1",
            timestamp=1,
        )

    outcome = install_packaged_skill(
        workdir=workdir,
        dest=dest,
        source=source,
        hmac_key=_KEY,
        install_id="i1",
        timestamp=1,
        force=True,
    )
    assert outcome.skill_hash == tree_content_hash(source)


def test_record_only_anchors_existing_tree_without_copying(tmp_path: Path) -> None:
    """A host-performed install (e.g. a plugin checkout) is anchorable post hoc."""
    workdir = tmp_path / "proj"
    workdir.mkdir()
    dest = tmp_path / "plugins" / "bernstein"
    _write_tree(dest, {".plugin/plugin.json": '{"name": "bernstein"}\n', "commands/run.md": "run\n"})

    outcome = install_packaged_skill(
        workdir=workdir,
        dest=dest,
        source=None,
        hmac_key=_KEY,
        install_id="plugin-claude",
        timestamp=7,
        record_only=True,
    )
    assert not outcome.copied
    assert outcome.skill_hash == tree_content_hash(dest)
    assert outcome.manifest_path == ".plugin/plugin.json"
    assert read_install_receipt(workdir, outcome.skill_hash) is not None


def test_install_rejects_symlinked_dest(tmp_path: Path) -> None:
    """A symlinked destination is refused before any copy, so the install can
    never write into the link target outside the requested path (security,
    issue #2642)."""
    workdir = tmp_path / "proj"
    workdir.mkdir()
    source = _skill_fixture(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    dest_parent = tmp_path / "host"
    dest_parent.mkdir()
    dest = dest_parent / PACKAGED_SKILL_NAME
    dest.symlink_to(outside, target_is_directory=True)

    with pytest.raises(PackagedInstallError):
        install_packaged_skill(
            workdir=workdir,
            dest=dest,
            source=source,
            hmac_key=_KEY,
            install_id="i1",
            timestamp=1,
        )
    # The copy never ran: nothing leaked into the symlink target.
    assert not any(outside.iterdir())


def test_record_only_missing_dest_raises(tmp_path: Path) -> None:
    with pytest.raises(PackagedInstallError):
        install_packaged_skill(
            workdir=tmp_path,
            dest=tmp_path / "absent",
            source=None,
            hmac_key=_KEY,
            install_id="i1",
            timestamp=1,
            record_only=True,
        )


# ---------------------------------------------------------------------------
# Verify: recompute + tamper detection
# ---------------------------------------------------------------------------


def test_verify_after_install_is_ok(tmp_path: Path) -> None:
    workdir = tmp_path / "proj"
    workdir.mkdir()
    source = _skill_fixture(tmp_path)
    dest = tmp_path / "host" / PACKAGED_SKILL_NAME
    install_packaged_skill(
        workdir=workdir,
        dest=dest,
        source=source,
        hmac_key=_KEY,
        install_id="i1",
        timestamp=1,
    )
    result = verify_packaged_install(workdir=workdir, dest=dest, hmac_key=_KEY)
    assert result.ok, result.reason


def test_verify_detects_installed_tree_tamper(tmp_path: Path) -> None:
    workdir = tmp_path / "proj"
    workdir.mkdir()
    source = _skill_fixture(tmp_path)
    dest = tmp_path / "host" / PACKAGED_SKILL_NAME
    install_packaged_skill(
        workdir=workdir,
        dest=dest,
        source=source,
        hmac_key=_KEY,
        install_id="i1",
        timestamp=1,
    )
    (dest / "SKILL.md").write_text("tampered\n", encoding="utf-8")
    result = verify_packaged_install(workdir=workdir, dest=dest, hmac_key=_KEY)
    assert not result.ok
    assert "receipt" in result.reason


def test_verify_detects_spine_tamper(tmp_path: Path) -> None:
    workdir = tmp_path / "proj"
    workdir.mkdir()
    source = _skill_fixture(tmp_path)
    dest = tmp_path / "host" / PACKAGED_SKILL_NAME
    install_packaged_skill(
        workdir=workdir,
        dest=dest,
        source=source,
        hmac_key=_KEY,
        install_id="i1",
        timestamp=1,
    )
    spine_path = workdir / ".sdd" / "lineage" / INSTALL_RUN_ID / "spine.jsonl"
    raw = spine_path.read_bytes()
    spine_path.write_bytes(
        raw.replace(b"agent_plugin", b"AGENT_PLUGIN") if b"agent_plugin" in raw else raw[:-2] + b"X\n"
    )
    result = verify_packaged_install(workdir=workdir, dest=dest, hmac_key=_KEY)
    assert not result.ok


def test_verify_missing_dest_reports_bad_input(tmp_path: Path) -> None:
    result = verify_packaged_install(workdir=tmp_path, dest=tmp_path / "absent", hmac_key=_KEY)
    assert not result.ok


def test_verify_rejects_receipt_without_chain_event(tmp_path: Path) -> None:
    """A receipt + spine written without the matching audit-chain event is a
    partial attestation and must not verify (data-integrity, issue #2642)."""
    from bernstein.core.skills.provenance import InstallReceipt, write_install_receipt

    workdir = tmp_path / "proj"
    workdir.mkdir()
    dest = tmp_path / "host" / PACKAGED_SKILL_NAME
    _write_tree(dest, {"SKILL.md": "---\nname: bernstein-run\n---\nbody\n"})
    skill_hash = tree_content_hash(dest)
    _, manifest_hash = manifest_hash_for(dest)

    # Anchor the receipt and spine directly, bypassing the audit-chain mirror
    # install_packaged_skill would otherwise write.
    write_install_receipt(
        workdir=workdir,
        lineage_root=workdir / ".sdd" / "lineage",
        hmac_key=_KEY,
        receipt=InstallReceipt(
            skill_hash=skill_hash,
            manifest_hash=manifest_hash,
            install_id="i1",
            timestamp=1,
        ),
    )

    result = verify_packaged_install(workdir=workdir, dest=dest, hmac_key=_KEY)
    assert not result.ok
    assert "chain" in result.reason.lower()


# ---------------------------------------------------------------------------
# Audit-chain event (direct)
# ---------------------------------------------------------------------------


def test_record_plugin_install_receipt_chains(tmp_path: Path) -> None:
    chain = AuditChainStore(tmp_path, key=_KEY)
    ev = record_plugin_install_receipt(
        chain=chain,
        skill_hash="sha256:" + "a" * 64,
        manifest_hash="b" * 64,
        install_id="i1",
        spine_anchor="sha256:" + "c" * 64,
        host="claude",
        scope="project",
        dest="/tmp/x",
    )
    assert ev.event_type == EVENT_PLUGIN_INSTALL_RECEIPT
    assert ev.details["host"] == "claude"
    assert ev.details["prev_chain_digest"]
    ok, errors = chain.verify()
    assert ok, errors
