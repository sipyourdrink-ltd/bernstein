"""Integration test: receipted compression is intentional, not drift (AC4, #2249).

End-to-end over the real modules (no mocks except the adapter rewrite):
a team manifest pins its role template digest, ``compress_role_templates``
rewrites the template with a chained receipt and a ``templates.lock``
row, and the team-manifest drift check (#2248) - both the library
classification and the ``bernstein team drift`` CLI - reports the
divergence as an intentional receipted compression instead of drift.
A manual edit on top of the compression is still reported as drift.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.main import cli
from bernstein.core.security.audit_chain import AuditChainStore
from bernstein.core.teams.drift import classify_role_template_drift, role_template_digest
from bernstein.core.teams.manifest import load_team_manifest
from bernstein.core.tokens.template_compression import (
    compress_role_templates,
    restore_role_templates,
)

pytestmark = pytest.mark.integration

ORIGINAL_SYSTEM = """\
# You are a Backend Engineer

## Your specialization
You implement server-side logic and APIs with great care and attention,
always reading the existing code before writing anything new at all.

## Rules
- Run `uv run ruff check src/` before completing

## Current task
{{TASK_DESCRIPTION}}
"""

COMPRESSED_SYSTEM = """\
# You are a Backend Engineer

## Your specialization
Server-side logic and APIs; read existing code first.

## Rules
- Run `uv run ruff check src/` before completing

## Current task
{{TASK_DESCRIPTION}}
"""

MANIFEST_TEMPLATE = """\
name = "crew"
version = "1.0.0"

[[roles]]
role = "backend"

[role_template_digests]
"backend" = "{digest}"
"""


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    role_dir = tmp_path / "templates" / "roles" / "backend"
    role_dir.mkdir(parents=True)
    (role_dir / "system_prompt.md").write_text(ORIGINAL_SYSTEM, encoding="utf-8")

    teams_dir = tmp_path / "templates" / "teams"
    teams_dir.mkdir(parents=True)
    (teams_dir / "crew.toml").write_text(
        MANIFEST_TEMPLATE.format(digest=role_template_digest(role_dir)),
        encoding="utf-8",
    )
    return tmp_path


def _compress(workdir: Path, tmp_path: Path) -> None:
    outcome = compress_role_templates(
        "backend",
        workdir=workdir,
        llm_call=lambda prompt: COMPRESSED_SYSTEM,
        adapter="openrouter",
        model="test-model",
        chain=AuditChainStore(tmp_path / "audit", key=b"k" * 32),
        backup_root=tmp_path / "backups",
    )
    assert outcome.applied, outcome.reason


class TestDriftRecognizesReceiptedCompression:
    def test_classification_marks_compression_intentional(self, workdir: Path, tmp_path: Path) -> None:
        _compress(workdir, tmp_path)
        manifest = load_team_manifest(workdir / "templates" / "teams" / "crew.toml")
        findings = classify_role_template_drift(manifest, workdir=workdir)
        assert set(findings) == {"backend"}
        finding = findings["backend"]
        assert finding.intentional
        assert finding.pinned_digest == manifest.role_template_digests["backend"]
        assert finding.actual_digest == role_template_digest(workdir / "templates" / "roles" / "backend")

    def test_team_drift_cli_reports_intentional_and_exits_zero(self, workdir: Path, tmp_path: Path) -> None:
        _compress(workdir, tmp_path)
        result = CliRunner().invoke(cli, ["team", "drift", "crew", "--workdir", str(workdir)])
        assert result.exit_code == 0, result.output
        assert "intentional" in result.output
        assert "receipted template compression" in result.output
        assert "drift detected" not in result.output

    def test_manual_edit_on_top_of_compression_is_still_drift(self, workdir: Path, tmp_path: Path) -> None:
        _compress(workdir, tmp_path)
        prompt = workdir / "templates" / "roles" / "backend" / "system_prompt.md"
        prompt.write_text(prompt.read_text(encoding="utf-8") + "x", encoding="utf-8")
        result = CliRunner().invoke(cli, ["team", "drift", "crew", "--workdir", str(workdir)])
        assert result.exit_code == 1, result.output
        assert "drift detected" in result.output

    def test_uncompressed_manual_edit_is_drift(self, workdir: Path) -> None:
        prompt = workdir / "templates" / "roles" / "backend" / "system_prompt.md"
        prompt.write_text(prompt.read_text(encoding="utf-8") + "x", encoding="utf-8")
        manifest = load_team_manifest(workdir / "templates" / "teams" / "crew.toml")
        findings = classify_role_template_drift(manifest, workdir=workdir)
        assert not findings["backend"].intentional

    def test_restore_returns_to_pinned_digest_no_drift(self, workdir: Path, tmp_path: Path) -> None:
        _compress(workdir, tmp_path)
        restore_role_templates("backend", workdir=workdir, backup_root=tmp_path / "backups")
        result = CliRunner().invoke(cli, ["team", "drift", "crew", "--workdir", str(workdir)])
        assert result.exit_code == 0, result.output
        assert "no drift detected" in result.output
