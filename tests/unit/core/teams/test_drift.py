"""Tests for bernstein.core.teams.drift (issue #2248, acceptance criterion 2)."""

from __future__ import annotations

from pathlib import Path

from bernstein.core.teams.drift import (
    detect_role_template_drift,
    role_template_digest,
)
from bernstein.core.teams.manifest import TeamCoordination, TeamManifest, TeamRoleSpec


def _write_role(tmp_path: Path, role: str, prompt: str = "# You are a specialist\n") -> Path:
    role_dir = tmp_path / "templates" / "roles" / role
    role_dir.mkdir(parents=True, exist_ok=True)
    (role_dir / "config.yaml").write_text("default_model: sonnet\n", encoding="utf-8")
    (role_dir / "system_prompt.md").write_text(prompt, encoding="utf-8")
    return role_dir


def _manifest_for(tmp_path: Path, roles: list[str]) -> TeamManifest:
    digests = {role: role_template_digest(tmp_path / "templates" / "roles" / role) for role in roles}
    return TeamManifest(
        name="t",
        version="1",
        roles=tuple(TeamRoleSpec(role=r, model_policy={}) for r in roles),
        coordination=TeamCoordination(),
        role_template_digests=digests,
    )


class TestRoleTemplateDigest:
    def test_digest_is_stable_across_calls(self, tmp_path: Path) -> None:
        role_dir = _write_role(tmp_path, "backend")
        assert role_template_digest(role_dir) == role_template_digest(role_dir)

    def test_one_byte_edit_changes_digest(self, tmp_path: Path) -> None:
        role_dir = _write_role(tmp_path, "backend")
        before = role_template_digest(role_dir)
        prompt = role_dir / "system_prompt.md"
        prompt.write_text(prompt.read_text(encoding="utf-8") + "x", encoding="utf-8")
        assert role_template_digest(role_dir) != before

    def test_hidden_files_are_ignored(self, tmp_path: Path) -> None:
        role_dir = _write_role(tmp_path, "backend")
        before = role_template_digest(role_dir)
        (role_dir / ".DS_Store").write_bytes(b"\x00\x01")
        assert role_template_digest(role_dir) == before

    def test_file_rename_changes_digest(self, tmp_path: Path) -> None:
        role_dir = _write_role(tmp_path, "backend")
        before = role_template_digest(role_dir)
        (role_dir / "system_prompt.md").rename(role_dir / "prompt.md")
        assert role_template_digest(role_dir) != before


class TestDetectDrift:
    def test_no_drift_when_templates_match_pins(self, tmp_path: Path) -> None:
        _write_role(tmp_path, "backend")
        _write_role(tmp_path, "qa")
        manifest = _manifest_for(tmp_path, ["backend", "qa"])
        assert detect_role_template_drift(manifest, workdir=tmp_path) == {}

    def test_one_byte_edit_is_detected(self, tmp_path: Path) -> None:
        _write_role(tmp_path, "backend")
        _write_role(tmp_path, "qa")
        manifest = _manifest_for(tmp_path, ["backend", "qa"])
        prompt = tmp_path / "templates" / "roles" / "qa" / "system_prompt.md"
        prompt.write_text(prompt.read_text(encoding="utf-8") + "!", encoding="utf-8")
        drift = detect_role_template_drift(manifest, workdir=tmp_path)
        assert set(drift) == {"qa"}
        pinned, actual = drift["qa"]
        assert pinned != actual

    def test_missing_role_directory_is_reported(self, tmp_path: Path) -> None:
        _write_role(tmp_path, "backend")
        manifest = _manifest_for(tmp_path, ["backend"])
        (tmp_path / "templates" / "roles" / "backend" / "config.yaml").unlink()
        (tmp_path / "templates" / "roles" / "backend" / "system_prompt.md").unlink()
        (tmp_path / "templates" / "roles" / "backend").rmdir()
        # An empty roles tree falls back to the packaged templates; pin a
        # role that exists nowhere to observe the missing marker.
        (tmp_path / "templates" / "roles").mkdir(exist_ok=True)
        object.__setattr__(manifest, "role_template_digests", {"no-such-role": "ab" * 32})
        drift = detect_role_template_drift(manifest, workdir=tmp_path)
        assert drift["no-such-role"][1] == "<missing>"

    def test_unpinned_roles_are_not_checked(self, tmp_path: Path) -> None:
        _write_role(tmp_path, "backend")
        _write_role(tmp_path, "qa")
        manifest = _manifest_for(tmp_path, ["backend"])
        prompt = tmp_path / "templates" / "roles" / "qa" / "system_prompt.md"
        prompt.write_text("changed", encoding="utf-8")
        assert detect_role_template_drift(manifest, workdir=tmp_path) == {}
