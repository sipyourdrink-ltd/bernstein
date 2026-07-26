"""Unit tests for the skill catalog (issue #1796).

Covers the acceptance criteria:
- browse render, search match
- signature-failed refusal
- install-from-github fixture
- drift detection
- lockfile atomicity
- cross-worktree consistency
- CI lineage gate behaviour
"""

from __future__ import annotations

import ast
import textwrap
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from bernstein.core.lineage.gate import check_skill_lockfile
from bernstein.core.plugins_core.plugin_installer import PluginInstallResult
from bernstein.core.security.audit import AuditLog
from bernstein.core.skills.catalog import (
    CATALOG_LOCK_FILENAME,
    RECEIPT_ADOPT,
    RECEIPT_INSTALL,
    RECEIPT_PIN,
    CatalogLockEntry,
    ManifestSignatureError,
    SkillCatalog,
    SkillCatalogAuditor,
    SkillCatalogEntry,
    SkillCatalogError,
    SkillCatalogService,
    SkillCatalogServiceConfig,
    SkillCatalogValidationError,
    SkillSourceSpec,
    canonical_entry_bytes,
    compute_manifest_sha256,
    detect_drift,
    generate_signer_keypair,
    read_state,
    record_pin,
    remove_catalog_entry,
    sign_entry,
    upsert_catalog_install,
    validate_catalog,
    verify_entry,
    worktree_id_for,
)
from bernstein.core.skills.catalog.signature import attach_signature

REPO_ROOT = Path(__file__).resolve().parents[4]
CATALOG_INSTALLER_PATH = Path("src/bernstein/core/skills/catalog/installer.py")

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_skill_dir(root: Path, *, name: str, description: str, body: str = "Body.") -> Path:
    """Materialise a SKILL.md tree the catalog installer expects."""
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        textwrap.dedent(
            f"""
            ---
            name: {name}
            description: {description}
            ---

            {body}
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    return skill_dir


def _make_entry(
    *,
    entry_id: str = "code-review",
    name: str = "code-review",
    version: str = "1.0.0",
    content_digest: str = "a" * 64,
    source: SkillSourceSpec | None = None,
    signature: str | None = None,
    verified: bool = True,
) -> SkillCatalogEntry:
    """Build a catalog entry with deterministic defaults."""
    return SkillCatalogEntry(
        id=entry_id,
        name=name,
        version=version,
        description=f"{name} catalog entry used by unit tests",
        source=source or SkillSourceSpec(kind="github", repo="acme/code-review", tag="v1.0.0"),
        content_digest=content_digest,
        signature=signature,
        homepage="",
        tags=("review",),
        verified=verified,
    )


def _make_catalog(
    entries: tuple[SkillCatalogEntry, ...] | None = None,
    *,
    signer_pubkey: str | None = None,
) -> SkillCatalog:
    """Wrap entries into a catalog with a stable generated_at timestamp."""
    if entries is None:
        entries = (_make_entry(),)
    return SkillCatalog(
        version=1,
        generated_at="2026-05-21T00:00:00Z",
        entries=entries,
        signer_pubkey=signer_pubkey,
    )


def _audit_dir(tmp_path: Path) -> Path:
    """Return an isolated audit dir with a writable key path."""
    d = tmp_path / "audit"
    d.mkdir()
    return d


@pytest.fixture(autouse=True)
def isolate_audit_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Force AuditLog to use a tmp-key so tests don't touch ~/.local/."""
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_validate_catalog_accepts_minimum_payload() -> None:
    payload = {
        "version": 1,
        "generated_at": "2026-05-21T00:00:00Z",
        "entries": [
            {
                "id": "code-review",
                "name": "code-review",
                "version": "1.0.0",
                "description": "Review code.",
                "source": {
                    "kind": "github",
                    "repo": "acme/code-review",
                    "tag": "v1.0.0",
                },
                "content_digest": "f" * 64,
                "verified": True,
            }
        ],
    }
    catalog = validate_catalog(payload)
    assert catalog.entries[0].id == "code-review"
    assert catalog.entries[0].source.kind == "github"


def test_validate_catalog_rejects_unknown_top_level_field() -> None:
    payload = {
        "version": 1,
        "generated_at": "2026-05-21T00:00:00Z",
        "entries": [],
        "rogue": "extra",
    }
    with pytest.raises(SkillCatalogValidationError):
        validate_catalog(payload)


def test_validate_catalog_rejects_unknown_source_kind() -> None:
    payload = {
        "version": 1,
        "generated_at": "2026-05-21T00:00:00Z",
        "entries": [
            {
                "id": "x",
                "name": "x",
                "version": "1",
                "description": "x",
                "source": {"kind": "torrent", "url": "tracker://x"},
                "content_digest": "f" * 64,
                "verified": False,
            }
        ],
    }
    with pytest.raises(SkillCatalogValidationError):
        validate_catalog(payload)


def test_validate_catalog_requires_hex_content_digest() -> None:
    payload = {
        "version": 1,
        "generated_at": "2026-05-21T00:00:00Z",
        "entries": [
            {
                "id": "x",
                "name": "x",
                "version": "1",
                "description": "x",
                "source": {"kind": "directory", "path": "/tmp/x"},
                "content_digest": "not-hex",
                "verified": False,
            }
        ],
    }
    with pytest.raises(SkillCatalogValidationError):
        validate_catalog(payload)


# ---------------------------------------------------------------------------
# Browse / search
# ---------------------------------------------------------------------------


def test_browse_returns_preloaded_catalog(tmp_path: Path) -> None:
    catalog = _make_catalog()
    config = SkillCatalogServiceConfig(workdir=tmp_path)
    service = SkillCatalogService(
        config=config,
        preloaded_catalog=catalog,
    )
    out = service.browse()
    assert out.entries[0].id == "code-review"


def test_search_substring_matches_tags_and_description(tmp_path: Path) -> None:
    entries = (
        _make_entry(entry_id="code-review", name="code-review"),
        SkillCatalogEntry(
            id="git-hygiene",
            name="git-hygiene",
            version="1.0.0",
            description="git-hygiene catalog entry used by unit tests",
            source=SkillSourceSpec(kind="github", repo="acme/git-hygiene", tag="v1.0.0"),
            content_digest="b" * 64,
            signature=None,
            homepage="",
            tags=("git",),
            verified=True,
        ),
    )
    catalog = _make_catalog(entries)
    config = SkillCatalogServiceConfig(workdir=tmp_path)
    service = SkillCatalogService(config=config, preloaded_catalog=catalog)
    assert [e.id for e in service.search("review")] == ["code-review"]
    assert {e.id for e in service.search("entry")} == {"code-review", "git-hygiene"}
    assert service.search("nonexistent") == []


# ---------------------------------------------------------------------------
# Signature
# ---------------------------------------------------------------------------


def test_sign_and_verify_roundtrip() -> None:
    priv, pub = generate_signer_keypair()
    entry = _make_entry()
    signature = sign_entry(entry, priv)
    signed = attach_signature(entry, signature)
    outcome = verify_entry(signed, pub)
    assert outcome.verified is True


def test_verify_entry_refuses_mismatched_key() -> None:
    priv_a, _ = generate_signer_keypair()
    _, pub_b = generate_signer_keypair()
    entry = attach_signature(_make_entry(), sign_entry(_make_entry(), priv_a))
    with pytest.raises(ManifestSignatureError):
        verify_entry(entry, pub_b)


def test_verify_entry_allows_unverified_with_flag() -> None:
    outcome = verify_entry(_make_entry(signature=None), None, allow_unverified=True)
    assert outcome.verified is False
    assert "no signature" in outcome.reason


def test_install_refuses_unverified_by_default(tmp_path: Path) -> None:
    """Acceptance: install refuses unverified manifests unless --allow-unverified."""
    _priv, pub = generate_signer_keypair()
    entry = _make_entry(signature=None)  # no signature
    catalog = _make_catalog((entry,), signer_pubkey=pub)
    config = SkillCatalogServiceConfig(workdir=tmp_path)
    service = SkillCatalogService(config=config, preloaded_catalog=catalog)
    with pytest.raises(ManifestSignatureError):
        service.install("code-review")


def test_install_refuses_tampered_signature(tmp_path: Path) -> None:
    """A signature that does not verify must reject the install."""
    priv, pub = generate_signer_keypair()
    entry = _make_entry()
    sig = sign_entry(entry, priv)
    # Corrupt the leading character. Flipping the first base64 char always
    # changes the high-order bits of the first decoded signature byte, so
    # verification fails deterministically. Flipping the trailing char can be
    # a no-op because base64 trailing bits are redundant, which let some
    # random keypairs slip a still-valid signature through to the installer.
    tampered = ("B" if sig[0] == "A" else "A") + sig[1:]
    signed_entry = attach_signature(entry, tampered)
    catalog = _make_catalog((signed_entry,), signer_pubkey=pub)
    config = SkillCatalogServiceConfig(workdir=tmp_path)
    service = SkillCatalogService(config=config, preloaded_catalog=catalog)
    with pytest.raises(ManifestSignatureError):
        service.install("code-review")


# ---------------------------------------------------------------------------
# Install (with mocked plugin installer fixture)
# ---------------------------------------------------------------------------


FIXTURE_DESCRIPTION = "Fixture catalog entry installed from github."


@pytest.fixture
def github_fixture_installer(tmp_path: Path) -> Callable[..., PluginInstallResult]:
    """A plugin installer that copies a local fixture in place of github."""

    def _installer(source, install_dir):  # type: ignore[no-untyped-def]
        # Build the skill tree in the staging directory.
        _write_skill_dir(
            install_dir,
            name="code-review",
            description=FIXTURE_DESCRIPTION,
        )
        return PluginInstallResult(
            success=True,
            install_path=install_dir / "code-review",
            source_kind=source.kind,
        )

    return _installer


def _compute_fixture_digest(tmp_path: Path) -> str:
    """Compute the digest of the github fixture exactly as it will land."""
    staging = tmp_path / "_digest_staging"
    staging.mkdir(exist_ok=True)
    _write_skill_dir(staging, name="code-review", description=FIXTURE_DESCRIPTION)
    from bernstein.core.skills.lifecycle import compute_skill_digest

    return compute_skill_digest(staging / "code-review").digest


def test_install_from_github_fixture_writes_lockfile(
    tmp_path: Path,
    github_fixture_installer: Callable[..., PluginInstallResult],
) -> None:
    """Acceptance: install-from-github fixture lands in skill layout."""
    priv, pub = generate_signer_keypair()
    expected_digest = _compute_fixture_digest(tmp_path)

    entry = _make_entry(content_digest=expected_digest)
    signature = sign_entry(entry, priv)
    signed = attach_signature(entry, signature)
    catalog = _make_catalog((signed,), signer_pubkey=pub)

    config = SkillCatalogServiceConfig(workdir=tmp_path)
    audit_dir = _audit_dir(tmp_path)
    service = SkillCatalogService(
        config=config,
        preloaded_catalog=catalog,
        auditor=SkillCatalogAuditor(audit_dir=audit_dir),
        plugin_installer=github_fixture_installer,
    )

    outcome = service.install("code-review")
    assert outcome.entry_id == "code-review"
    assert outcome.verified is True
    assert outcome.install_dir.is_dir()
    assert (outcome.install_dir / "SKILL.md").is_file()
    assert outcome.content_digest == expected_digest

    lockfile = tmp_path / CATALOG_LOCK_FILENAME
    state = read_state(lockfile)
    assert len(state.catalog) == 1
    row = state.catalog[0]
    assert row.id == "code-review"
    assert row.manifest_sha256 == compute_manifest_sha256(
        outcome.manifest_url,
        signed.to_dict(),
    )
    # Lineage receipt records the first install.
    assert any(r.action == RECEIPT_INSTALL for r in state.receipts)


def test_install_anchors_lineage_receipt_in_spine(
    tmp_path: Path,
    github_fixture_installer: Callable[..., PluginInstallResult],
) -> None:
    """AC1 (issue #2301): install writes a spine-anchored lineage receipt."""
    from bernstein.core.lineage.spine import LineageSpine
    from bernstein.core.security.audit import AuditLog, load_or_create_audit_key
    from bernstein.core.skills.provenance import (
        INSTALL_RUN_ID,
        read_install_receipt,
        verify_install,
    )

    priv, pub = generate_signer_keypair()
    expected_digest = _compute_fixture_digest(tmp_path)
    entry = _make_entry(content_digest=expected_digest)
    signed = attach_signature(entry, sign_entry(entry, priv))
    catalog = _make_catalog((signed,), signer_pubkey=pub)

    config = SkillCatalogServiceConfig(workdir=tmp_path)
    service = SkillCatalogService(
        config=config,
        preloaded_catalog=catalog,
        auditor=SkillCatalogAuditor(audit_dir=tmp_path / ".sdd" / "audit"),
        plugin_installer=github_fixture_installer,
    )
    outcome = service.install("code-review")

    # The receipt exists on disk and its skill_hash is the installed digest.
    receipt = read_install_receipt(tmp_path, outcome.content_digest)
    assert receipt is not None
    assert receipt.skill_hash == outcome.content_digest
    assert receipt.manifest_hash == outcome.manifest_sha256

    # The install spine verifies (AC1) and a chain event names the anchor.
    key = load_or_create_audit_key()
    spine = LineageSpine(tmp_path / ".sdd" / "lineage", run_id=INSTALL_RUN_ID, hmac_key=key)
    assert spine.verify().ok

    log = AuditLog(tmp_path / ".sdd" / "audit")
    events = log.query(event_type="skill.install_receipt")
    assert len(events) == 1
    assert events[0].details["spine_anchor"] == spine.head_hash()

    # AC5: verify passes when installed manifest matches, fails on drift.
    ok = verify_install(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=key,
        skill_hash=outcome.content_digest,
        installed_manifest_hash=outcome.manifest_sha256,
    )
    assert ok.ok
    drift = verify_install(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=key,
        skill_hash=outcome.content_digest,
        installed_manifest_hash="d" * 64,
    )
    assert not drift.ok


def test_install_refuses_content_digest_mismatch(
    tmp_path: Path,
    github_fixture_installer: Callable[..., PluginInstallResult],
) -> None:
    """An installer that lands different bytes than the manifest claims is rejected."""
    priv, pub = generate_signer_keypair()
    entry = _make_entry(content_digest="b" * 64)  # wrong
    signed = attach_signature(entry, sign_entry(entry, priv))
    catalog = _make_catalog((signed,), signer_pubkey=pub)

    config = SkillCatalogServiceConfig(workdir=tmp_path)
    service = SkillCatalogService(
        config=config,
        preloaded_catalog=catalog,
        auditor=SkillCatalogAuditor(audit_dir=_audit_dir(tmp_path)),
        plugin_installer=github_fixture_installer,
    )
    with pytest.raises(SkillCatalogError, match="content digest"):
        service.install("code-review")


def test_catalog_install_refuses_invisible_unicode_skill(tmp_path: Path) -> None:
    """Catalog installs inherit the local install-time Unicode gate."""

    def poisoned_installer(source, install_dir):  # type: ignore[no-untyped-def]
        _write_skill_dir(
            install_dir,
            name="code-review",
            description=FIXTURE_DESCRIPTION,
            body="# Code review\n\U000e0048\n",
        )
        return PluginInstallResult(
            success=True,
            install_path=install_dir / "code-review",
            source_kind=source.kind,
        )

    priv, pub = generate_signer_keypair()
    entry = _make_entry(content_digest="a" * 64)
    signed = attach_signature(entry, sign_entry(entry, priv))
    catalog = _make_catalog((signed,), signer_pubkey=pub)

    config = SkillCatalogServiceConfig(workdir=tmp_path)
    service = SkillCatalogService(
        config=config,
        preloaded_catalog=catalog,
        auditor=SkillCatalogAuditor(audit_dir=_audit_dir(tmp_path)),
        plugin_installer=poisoned_installer,
    )

    with pytest.raises(SkillCatalogError, match="invisible Unicode"):
        service.install("code-review")


# ---------------------------------------------------------------------------
# Drift
# ---------------------------------------------------------------------------


def test_detect_drift_flags_missing_and_changed_digests(tmp_path: Path) -> None:
    lockfile = tmp_path / CATALOG_LOCK_FILENAME
    entry = CatalogLockEntry(
        id="code-review",
        name="code-review",
        version="1.0.0",
        manifest_url="github://acme/code-review@v1",
        manifest_sha256="1" * 64,
        content_digest="a" * 64,
        install_id="iid",
        chain_head="c" * 64,
        installed_at="2026-05-21T00:00:00Z",
    )
    upsert_catalog_install(lockfile, entry, workdir=tmp_path)
    # Drift: claim a different installed digest.
    drift = detect_drift(lockfile, {"code-review": "b" * 64})
    assert drift == {"code-review": ("a" * 64, "b" * 64)}
    # Missing install also reported.
    drift_missing = detect_drift(lockfile, {})
    assert drift_missing == {"code-review": ("a" * 64, "<missing>")}


# ---------------------------------------------------------------------------
# Lockfile atomicity
# ---------------------------------------------------------------------------


def test_lockfile_atomic_swap_uses_tempfile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Acceptance: lockfile updates atomically.

    We assert by intercepting :meth:`Path.replace` and inspecting that the
    source path is a sibling `.tmp` file (atomic swap), never the lockfile
    itself.
    """
    lockfile = tmp_path / CATALOG_LOCK_FILENAME
    swaps: list[tuple[Path, Path]] = []
    real_replace = Path.replace

    def _capture(self: Path, target):  # type: ignore[no-untyped-def]
        swaps.append((Path(self), Path(target)))
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", _capture)
    entry = CatalogLockEntry(
        id="atomic",
        name="atomic",
        version="0.0.1",
        manifest_url="directory:///tmp",
        manifest_sha256="d" * 64,
        content_digest="e" * 64,
        install_id="iid",
        chain_head="0" * 64,
        installed_at="2026-05-21T00:00:00Z",
    )
    upsert_catalog_install(lockfile, entry, workdir=tmp_path)
    # The swap target must be the lockfile and the source must be a tmp sibling.
    matching = [s for s in swaps if s[1] == lockfile]
    assert matching, "no atomic swap recorded"
    assert all(src.suffix == ".tmp" for src, _ in matching)


def test_lockfile_drops_uninstalled_entry(tmp_path: Path) -> None:
    lockfile = tmp_path / CATALOG_LOCK_FILENAME
    entry = CatalogLockEntry(
        id="ephemeral",
        name="ephemeral",
        version="0.0.1",
        manifest_url="directory:///tmp",
        manifest_sha256="d" * 64,
        content_digest="e" * 64,
        install_id="iid",
        chain_head="0" * 64,
        installed_at="2026-05-21T00:00:00Z",
    )
    upsert_catalog_install(lockfile, entry, workdir=tmp_path)
    remove_catalog_entry(lockfile, "ephemeral")
    state = read_state(lockfile)
    assert state.catalog == []


# ---------------------------------------------------------------------------
# Cross-worktree consistency
# ---------------------------------------------------------------------------


def test_cross_worktree_consistency_same_chain_head_yields_same_digest(
    tmp_path: Path,
) -> None:
    """Acceptance: two parallel worktrees launched from the same chain head
    observe identical skill versions.

    We simulate by writing the same lockfile rows into two separate
    worktree paths and asserting their catalog digests are identical.
    """
    wt_a = tmp_path / "wt-a"
    wt_b = tmp_path / "wt-b"
    wt_a.mkdir()
    wt_b.mkdir()
    entry = CatalogLockEntry(
        id="shared",
        name="shared",
        version="1.0.0",
        manifest_url="github://acme/shared@v1",
        manifest_sha256="1" * 64,
        content_digest="2" * 64,
        install_id="install-1",
        chain_head="cafe" + "0" * 60,
        installed_at="2026-05-21T00:00:00Z",
    )
    upsert_catalog_install(wt_a / CATALOG_LOCK_FILENAME, entry, workdir=wt_a)
    upsert_catalog_install(wt_b / CATALOG_LOCK_FILENAME, entry, workdir=wt_b)

    state_a = read_state(wt_a / CATALOG_LOCK_FILENAME)
    state_b = read_state(wt_b / CATALOG_LOCK_FILENAME)
    assert state_a.digest() == state_b.digest()


def test_upgrade_emits_adopt_receipt_then_sibling_can_pin(tmp_path: Path) -> None:
    """Acceptance: an upgrade in wt-a produces a lineage receipt that lets
    wt-b decide deterministically whether to adopt or pin."""
    wt_a_lock = tmp_path / "wt-a" / CATALOG_LOCK_FILENAME
    wt_b_lock = tmp_path / "wt-b" / CATALOG_LOCK_FILENAME
    wt_a_lock.parent.mkdir()
    wt_b_lock.parent.mkdir()

    initial = CatalogLockEntry(
        id="shared",
        name="shared",
        version="1.0.0",
        manifest_url="github://acme/shared@v1",
        manifest_sha256="1" * 64,
        content_digest="2" * 64,
        install_id="install-1",
        chain_head="1111" + "0" * 60,
        installed_at="2026-05-21T00:00:00Z",
    )
    upgraded = replace(
        initial,
        version="1.1.0",
        manifest_url="github://acme/shared@v1.1.0",
        manifest_sha256="3" * 64,
        install_id="install-2",
        chain_head="2222" + "0" * 60,
    )

    # wt-a: first install, then upgrade.
    upsert_catalog_install(wt_a_lock, initial, workdir=wt_a_lock.parent)
    upsert_catalog_install(wt_a_lock, upgraded, workdir=wt_a_lock.parent)
    state_a = read_state(wt_a_lock)
    actions = [r.action for r in state_a.receipts]
    assert RECEIPT_INSTALL in actions
    assert RECEIPT_ADOPT in actions

    # wt-b: first install at the same chain head, then choose to pin.
    upsert_catalog_install(wt_b_lock, initial, workdir=wt_b_lock.parent)
    pin_state = record_pin(
        wt_b_lock,
        entry_id="shared",
        chain_head="1111" + "0" * 60,
        manifest_sha256="1" * 64,
        workdir=wt_b_lock.parent,
    )
    assert pin_state.receipts[-1].action == RECEIPT_PIN
    # Worktree ids are distinct so audit can attribute decisions.
    assert worktree_id_for(wt_a_lock.parent) != worktree_id_for(wt_b_lock.parent)


# ---------------------------------------------------------------------------
# CI lineage gate
# ---------------------------------------------------------------------------


def test_lineage_gate_accepts_anchored_lockfile(tmp_path: Path) -> None:
    """Acceptance: CI lineage gate accepts a lockfile whose shas are anchored."""
    lockfile = tmp_path / CATALOG_LOCK_FILENAME
    sha = "deadbeef" + "0" * 56
    entry = CatalogLockEntry(
        id="code-review",
        name="code-review",
        version="1.0.0",
        manifest_url="github://acme/code-review@v1",
        manifest_sha256=sha,
        content_digest="2" * 64,
        install_id="install-1",
        chain_head="3" * 64,
        installed_at="2026-05-21T00:00:00Z",
    )
    upsert_catalog_install(lockfile, entry, workdir=tmp_path)
    result = check_skill_lockfile(lockfile, frozenset({sha}))
    assert result.ok
    assert result.failures == []


def test_lineage_gate_rejects_unanchored_lockfile(tmp_path: Path) -> None:
    """Acceptance: CI lineage gate rejects a lockfile referencing an unknown sha."""
    lockfile = tmp_path / CATALOG_LOCK_FILENAME
    entry = CatalogLockEntry(
        id="code-review",
        name="code-review",
        version="1.0.0",
        manifest_url="github://acme/code-review@v1",
        manifest_sha256="rogue" + "0" * 59,
        content_digest="2" * 64,
        install_id="install-1",
        chain_head="3" * 64,
        installed_at="2026-05-21T00:00:00Z",
    )
    upsert_catalog_install(lockfile, entry, workdir=tmp_path)
    result = check_skill_lockfile(lockfile, frozenset({"different" + "0" * 55}))
    assert not result.ok
    assert any("code-review" in f for f in result.failures)


def test_lineage_gate_skips_missing_lockfile(tmp_path: Path) -> None:
    """Missing lockfile is a no-op pass; no PR-blocking churn."""
    result = check_skill_lockfile(tmp_path / "nonexistent.lock", frozenset())
    assert result.ok


# ---------------------------------------------------------------------------
# Audit chain integration
# ---------------------------------------------------------------------------


def test_install_appends_chain_entry_with_required_fields(
    tmp_path: Path,
    github_fixture_installer: Callable[..., PluginInstallResult],
) -> None:
    """The install path appends a `skill.catalog.install` event with the
    required (manifest_url, manifest_sha256, manifest_signer_pubkey,
    install_id, prior_install_chain_head) tuple.

    The per-entry link is deliberately not called ``prev_chain_digest``:
    that key means "this record's own chain predecessor", which the
    previous install of the same entry is not (#3062).
    """
    priv, pub = generate_signer_keypair()

    expected_digest = _compute_fixture_digest(tmp_path)
    entry = _make_entry(content_digest=expected_digest)
    signed = attach_signature(entry, sign_entry(entry, priv))
    catalog = _make_catalog((signed,), signer_pubkey=pub)

    audit_dir = _audit_dir(tmp_path)
    auditor = SkillCatalogAuditor(audit_dir=audit_dir)
    config = SkillCatalogServiceConfig(workdir=tmp_path)
    service = SkillCatalogService(
        config=config,
        preloaded_catalog=catalog,
        auditor=auditor,
        plugin_installer=github_fixture_installer,
    )
    service.install("code-review")

    # Reopen the audit log to query the chain.
    log = AuditLog(audit_dir)
    events = log.query(event_type="skill.catalog.install")
    assert len(events) == 1
    details = events[0].details
    assert details["manifest_url"]
    assert details["manifest_sha256"]
    assert details["manifest_signer_pubkey"] == pub
    assert details["install_id"]
    assert details["prior_install_chain_head"]
    assert "prev_chain_digest" not in details


def test_replay_refuses_when_upstream_sha_drifted(
    tmp_path: Path,
    github_fixture_installer: Callable[..., PluginInstallResult],
) -> None:
    """Acceptance: reverting and re-running the chain pulls the same sha;
    install refuses if the upstream sha drifted."""
    priv, pub = generate_signer_keypair()

    expected_digest = _compute_fixture_digest(tmp_path)
    entry_v1 = _make_entry(content_digest=expected_digest, version="1.0.0")
    signed_v1 = attach_signature(entry_v1, sign_entry(entry_v1, priv))
    catalog_v1 = _make_catalog((signed_v1,), signer_pubkey=pub)

    audit_dir = _audit_dir(tmp_path)
    auditor = SkillCatalogAuditor(audit_dir=audit_dir)
    config = SkillCatalogServiceConfig(workdir=tmp_path)
    service_v1 = SkillCatalogService(
        config=config,
        preloaded_catalog=catalog_v1,
        auditor=auditor,
        plugin_installer=github_fixture_installer,
    )
    service_v1.install("code-review")

    # Now simulate upstream drift: same id but DIFFERENT description (which
    # changes the canonical entry payload and therefore the manifest sha).
    drifted = _make_entry(
        content_digest=expected_digest,
        version="1.0.0",
    )
    drifted = SkillCatalogEntry(
        id=drifted.id,
        name=drifted.name,
        version=drifted.version,
        description="DRIFTED description.",
        source=drifted.source,
        content_digest=drifted.content_digest,
        signature=None,
        homepage=drifted.homepage,
        tags=drifted.tags,
        verified=drifted.verified,
    )
    signed_drifted = attach_signature(drifted, sign_entry(drifted, priv))
    catalog_drifted = _make_catalog((signed_drifted,), signer_pubkey=pub)
    service_drifted = SkillCatalogService(
        config=config,
        preloaded_catalog=catalog_drifted,
        auditor=SkillCatalogAuditor(audit_dir=audit_dir),
        plugin_installer=github_fixture_installer,
    )
    with pytest.raises(SkillCatalogError, match="drifted"):
        service_drifted.install("code-review")


def test_auditor_known_good_shas_reflect_chain(
    tmp_path: Path,
    github_fixture_installer: Callable[..., PluginInstallResult],
) -> None:
    """The auditor's known-good set drives the CI gate: every installed sha is
    present, no others."""
    priv, pub = generate_signer_keypair()
    expected_digest = _compute_fixture_digest(tmp_path)
    entry = _make_entry(content_digest=expected_digest)
    signed = attach_signature(entry, sign_entry(entry, priv))
    catalog = _make_catalog((signed,), signer_pubkey=pub)

    audit_dir = _audit_dir(tmp_path)
    auditor = SkillCatalogAuditor(audit_dir=audit_dir)
    config = SkillCatalogServiceConfig(workdir=tmp_path)
    service = SkillCatalogService(
        config=config,
        preloaded_catalog=catalog,
        auditor=auditor,
        plugin_installer=github_fixture_installer,
    )
    outcome = service.install("code-review")

    # Re-opening produces the same known-good set.
    reopened = SkillCatalogAuditor(audit_dir=audit_dir)
    known = reopened.known_good_manifest_shas()
    assert outcome.manifest_sha256 in known


# ---------------------------------------------------------------------------
# Canonical signing payload stability
# ---------------------------------------------------------------------------


def test_canonical_payload_excludes_signature_and_verified() -> None:
    """The bytes signed must NOT include the signature or verified flag.

    Otherwise the signature would be self-referential and `verified` (an
    operator-side concern) would change the payload upstream-side too.
    """
    entry = _make_entry(signature="abc", verified=True)
    bytes_a = canonical_entry_bytes(entry)
    bytes_b = canonical_entry_bytes(
        SkillCatalogEntry(
            id=entry.id,
            name=entry.name,
            version=entry.version,
            description=entry.description,
            source=entry.source,
            content_digest=entry.content_digest,
            signature="different",
            homepage=entry.homepage,
            tags=entry.tags,
            verified=False,
        )
    )
    assert bytes_a == bytes_b


def test_catalog_installer_has_no_redundant_catch_and_rethrow() -> None:
    """The catalog installer should let CatalogInstallError propagate directly."""
    source = (REPO_ROOT / CATALOG_INSTALLER_PATH).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(CATALOG_INSTALLER_PATH))

    redundant_handlers = [
        handler
        for handler in ast.walk(tree)
        if isinstance(handler, ast.ExceptHandler)
        and isinstance(handler.type, ast.Name)
        and handler.type.id == "CatalogInstallError"
        and len(handler.body) == 1
        and isinstance(handler.body[0], ast.Raise)
        and handler.body[0].exc is None
    ]

    assert not redundant_handlers
