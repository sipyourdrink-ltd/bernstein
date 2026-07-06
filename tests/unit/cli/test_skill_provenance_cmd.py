"""CLI tests for ``bernstein skill provenance`` / ``verify`` (issue #2301)."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.skill_cmd import skill_group
from bernstein.core.lineage.spine import LineageSpine
from bernstein.core.skills.provenance import (
    InstallReceipt,
    record_usage,
    write_install_receipt,
)

_KEY = b"0" * 32
_SKILL_HASH = "a" * 64
_MANIFEST_HASH = "b" * 64


@pytest.fixture(autouse=True)
def _isolate_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    key_file = tmp_path / "audit.key"
    key_file.write_bytes(_KEY)
    key_file.chmod(0o600)
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(key_file))


def _lineage_root(workdir: Path) -> Path:
    return workdir / ".sdd" / "lineage"


def _seed_run(workdir: Path, run_id: str) -> str:
    spine = LineageSpine(_lineage_root(workdir), run_id=run_id, hmac_key=_KEY)
    spine.record(
        artifact_path=f"out/{run_id}.txt",
        content=b"x",
        actor="w",
        step_id="s",
        model="m",
        timestamp=1,
    )
    return spine.head_hash()


def test_provenance_lists_verified_runs(tmp_path: Path) -> None:
    workdir = tmp_path / "proj"
    workdir.mkdir()
    head = _seed_run(workdir, "run-1")
    record_usage(
        workdir=workdir,
        skill_hash=_SKILL_HASH,
        run_id="run-1",
        journal_head=head,
        timestamp=1,
    )

    result = CliRunner().invoke(
        skill_group,
        ["provenance", _SKILL_HASH, "--workdir", str(workdir)],
    )
    assert result.exit_code == 0
    assert "verified_runs=1" in result.output
    assert "run-1" in result.output


def test_provenance_empty_for_unknown_skill(tmp_path: Path) -> None:
    workdir = tmp_path / "proj"
    workdir.mkdir()
    result = CliRunner().invoke(
        skill_group,
        ["provenance", _SKILL_HASH, "--workdir", str(workdir)],
    )
    assert result.exit_code == 0
    assert "No recorded usage" in result.output


def test_verify_ok_and_mismatch(tmp_path: Path) -> None:
    """AC5 via CLI: verify passes on match, exits 2 on manifest drift."""
    from bernstein.core.skills.catalog.lockfile import (
        CATALOG_LOCK_FILENAME,
        CatalogLockEntry,
        CatalogLockState,
        write_state,
    )

    workdir = tmp_path / "proj"
    workdir.mkdir()
    write_install_receipt(
        workdir=workdir,
        lineage_root=_lineage_root(workdir),
        hmac_key=_KEY,
        receipt=InstallReceipt(
            skill_hash=_SKILL_HASH,
            manifest_hash=_MANIFEST_HASH,
            install_id="i1",
            timestamp=1,
        ),
    )
    lock = workdir / CATALOG_LOCK_FILENAME
    write_state(
        lock,
        CatalogLockState(
            catalog=[
                CatalogLockEntry(
                    id="code-review",
                    name="code-review",
                    version="1.0.0",
                    manifest_url="github://acme/code-review@v1.0.0",
                    manifest_sha256=_MANIFEST_HASH,
                    content_digest=_SKILL_HASH,
                    install_id="i1",
                    chain_head="c" * 64,
                    installed_at="2026-07-06T00:00:00Z",
                )
            ],
        ),
    )

    ok = CliRunner().invoke(skill_group, ["verify", "code-review", "--workdir", str(workdir)])
    assert ok.exit_code == 0
    assert "OK" in ok.output

    # Drift the lockfile manifest hash -> verify recomputes and exits 2.
    write_state(
        lock,
        CatalogLockState(
            catalog=[
                CatalogLockEntry(
                    id="code-review",
                    name="code-review",
                    version="1.0.0",
                    manifest_url="github://acme/code-review@v1.0.0",
                    manifest_sha256="e" * 64,
                    content_digest=_SKILL_HASH,
                    install_id="i1",
                    chain_head="c" * 64,
                    installed_at="2026-07-06T00:00:00Z",
                )
            ],
        ),
    )
    drift = CliRunner().invoke(skill_group, ["verify", "code-review", "--workdir", str(workdir)])
    assert drift.exit_code == 2
    assert "MISMATCH" in drift.output


def test_verify_requires_lockfile_row(tmp_path: Path) -> None:
    """Without a lockfile row we cannot recompute the manifest hash."""
    workdir = tmp_path / "proj"
    workdir.mkdir()
    write_install_receipt(
        workdir=workdir,
        lineage_root=_lineage_root(workdir),
        hmac_key=_KEY,
        receipt=InstallReceipt(
            skill_hash=_SKILL_HASH,
            manifest_hash=_MANIFEST_HASH,
            install_id="i1",
            timestamp=1,
        ),
    )
    result = CliRunner().invoke(
        skill_group,
        ["verify", _SKILL_HASH, "--workdir", str(workdir)],
    )
    assert result.exit_code == 1
    assert "lockfile row" in result.output
