"""CLI tests for ``bernstein skills package`` (issue #2369)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.skills_package_cmd import package_group
from bernstein.core.skills.packaging import PACKAGED_SKILL_NAME, tree_content_hash
from bernstein.core.skills.provenance import read_install_receipt

if TYPE_CHECKING:
    from pathlib import Path

_KEY = b"0" * 32


@pytest.fixture(autouse=True)
def _isolate_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    key_file = tmp_path / "audit.key"
    key_file.write_bytes(_KEY)
    key_file.chmod(0o600)
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(key_file))


def _workdir(tmp_path: Path) -> Path:
    workdir = tmp_path / "proj"
    workdir.mkdir(exist_ok=True)
    return workdir


def test_show_prints_bundled_asset_identity(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        package_group,
        ["show", "--workdir", str(_workdir(tmp_path))],
    )
    assert result.exit_code == 0, result.output
    assert PACKAGED_SKILL_NAME in result.output
    assert "sha256:" in result.output


def test_install_to_dest_writes_receipt_and_verifies(tmp_path: Path) -> None:
    workdir = _workdir(tmp_path)
    dest = tmp_path / "skills-dir" / PACKAGED_SKILL_NAME

    result = CliRunner().invoke(
        package_group,
        ["install", "--dest", str(dest), "--workdir", str(workdir)],
    )
    assert result.exit_code == 0, result.output
    assert (dest / "SKILL.md").is_file()

    skill_hash = tree_content_hash(dest)
    assert read_install_receipt(workdir, skill_hash) is not None

    verify = CliRunner().invoke(
        package_group,
        ["verify", "--dest", str(dest), "--workdir", str(workdir)],
    )
    assert verify.exit_code == 0, verify.output


def test_dest_overrides_host_scope_in_audit_labels(tmp_path: Path) -> None:
    """An explicit --dest is documented to override --host/--scope; the audit
    event and install_id must record the override, not the arbitrary host/scope
    the operator also passed (data-integrity, issue #2642)."""
    from bernstein.core.security.audit_chain import (
        EVENT_PLUGIN_INSTALL_RECEIPT,
        AuditChainStore,
    )

    workdir = _workdir(tmp_path)
    dest = tmp_path / "explicit" / PACKAGED_SKILL_NAME
    result = CliRunner().invoke(
        package_group,
        [
            "install",
            "--dest",
            str(dest),
            "--host",
            "claude",
            "--scope",
            "user",
            "--workdir",
            str(workdir),
        ],
    )
    assert result.exit_code == 0, result.output

    chain = AuditChainStore(workdir / ".sdd" / "audit", key=_KEY)
    events = chain.query(event_type=EVENT_PLUGIN_INSTALL_RECEIPT)
    assert len(events) == 1
    assert events[0].details["host"] == "dest"
    assert events[0].details["scope"] == "dest"
    assert events[0].details["install_id"] == "agent-plugin-dest-dest"


def test_install_host_scope_project_targets_host_dir(tmp_path: Path) -> None:
    workdir = _workdir(tmp_path)
    result = CliRunner().invoke(
        package_group,
        ["install", "--host", "claude", "--scope", "project", "--workdir", str(workdir)],
    )
    assert result.exit_code == 0, result.output
    dest = workdir / ".claude" / "skills" / PACKAGED_SKILL_NAME
    assert (dest / "SKILL.md").is_file()


def test_verify_detects_tamper_with_exit_2(tmp_path: Path) -> None:
    workdir = _workdir(tmp_path)
    dest = tmp_path / "skills-dir" / PACKAGED_SKILL_NAME
    install = CliRunner().invoke(
        package_group,
        ["install", "--dest", str(dest), "--workdir", str(workdir)],
    )
    assert install.exit_code == 0, install.output

    (dest / "SKILL.md").write_text("tampered\n", encoding="utf-8")
    verify = CliRunner().invoke(
        package_group,
        ["verify", "--dest", str(dest), "--workdir", str(workdir)],
    )
    assert verify.exit_code == 2, verify.output


def test_verify_without_receipt_exits_1(tmp_path: Path) -> None:
    workdir = _workdir(tmp_path)
    dest = tmp_path / "unanchored"
    dest.mkdir()
    (dest / "SKILL.md").write_text("never installed\n", encoding="utf-8")
    result = CliRunner().invoke(
        package_group,
        ["verify", "--dest", str(dest), "--workdir", str(workdir)],
    )
    assert result.exit_code == 2, result.output


def test_install_record_only_anchors_plugin_checkout(tmp_path: Path) -> None:
    workdir = _workdir(tmp_path)
    checkout = tmp_path / "plugins" / "bernstein"
    (checkout / ".plugin").mkdir(parents=True)
    (checkout / ".plugin" / "plugin.json").write_text('{"name": "bernstein"}\n', encoding="utf-8")

    result = CliRunner().invoke(
        package_group,
        ["install", "--dest", str(checkout), "--record-only", "--workdir", str(workdir)],
    )
    assert result.exit_code == 0, result.output
    assert read_install_receipt(workdir, tree_content_hash(checkout)) is not None

    verify = CliRunner().invoke(
        package_group,
        ["verify", "--dest", str(checkout), "--workdir", str(workdir)],
    )
    assert verify.exit_code == 0, verify.output


def test_package_group_registered_under_skills() -> None:
    from bernstein.cli.main import cli

    skills = cli.commands["skills"]
    assert "package" in skills.commands  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


def _install_from_source(workdir: Path, dest: Path, source: Path) -> None:
    from bernstein.core.skills.packaging import install_packaged_skill

    install_packaged_skill(
        workdir=workdir,
        dest=dest,
        source=source,
        hmac_key=_KEY,
        install_id="agent-plugin-dest-dest",
        timestamp=100,
        host="dest",
        scope="dest",
    )


def _make_skill(root: Path, body: str) -> Path:
    src = root / f"src-{body}"
    src.mkdir(parents=True, exist_ok=True)
    (src / "SKILL.md").write_text(f"---\nname: bernstein-run\n---\n{body}\n", encoding="utf-8")
    return src


def test_update_supersedes_prior_install(tmp_path: Path) -> None:
    workdir = _workdir(tmp_path)
    dest = tmp_path / "skills-dir" / PACKAGED_SKILL_NAME
    _install_from_source(workdir, dest, _make_skill(tmp_path, "one"))

    result = CliRunner().invoke(
        package_group,
        ["update", "--dest", str(dest), "--source", str(_make_skill(tmp_path, "two")), "--workdir", str(workdir)],
    )
    assert result.exit_code == 0, result.output
    assert "updated" in result.output
    assert "two" in (dest / "SKILL.md").read_text(encoding="utf-8")

    verify = CliRunner().invoke(
        package_group,
        ["verify", "--dest", str(dest), "--workdir", str(workdir)],
    )
    assert verify.exit_code == 0, verify.output


def test_update_already_current_exits_0(tmp_path: Path) -> None:
    workdir = _workdir(tmp_path)
    dest = tmp_path / "skills-dir" / PACKAGED_SKILL_NAME
    src = _make_skill(tmp_path, "one")
    _install_from_source(workdir, dest, src)

    result = CliRunner().invoke(
        package_group,
        ["update", "--dest", str(dest), "--source", str(src), "--workdir", str(workdir)],
    )
    assert result.exit_code == 0, result.output
    assert "already current" in result.output


def test_update_unattested_tree_errors(tmp_path: Path) -> None:
    workdir = _workdir(tmp_path)
    dest = tmp_path / "skills-dir" / PACKAGED_SKILL_NAME
    dest.mkdir(parents=True)
    (dest / "SKILL.md").write_text("handmade\n", encoding="utf-8")

    result = CliRunner().invoke(
        package_group,
        ["update", "--dest", str(dest), "--source", str(_make_skill(tmp_path, "two")), "--workdir", str(workdir)],
    )
    assert result.exit_code == 1
    assert "attested" in result.output


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_status_reports_verified_install(tmp_path: Path) -> None:
    workdir = _workdir(tmp_path)
    result = CliRunner().invoke(
        package_group,
        ["install", "--host", "claude", "--scope", "project", "--workdir", str(workdir)],
    )
    assert result.exit_code == 0, result.output

    status = CliRunner().invoke(
        package_group,
        ["status", "--workdir", str(workdir), "--home", str(tmp_path / "home")],
    )
    assert status.exit_code == 0, status.output
    assert "claude" in status.output
    assert "OK" in status.output


def test_status_flags_tampered_install_with_exit_2(tmp_path: Path) -> None:
    workdir = _workdir(tmp_path)
    CliRunner().invoke(
        package_group,
        ["install", "--host", "claude", "--scope", "project", "--workdir", str(workdir)],
    )
    dest = workdir / ".claude" / "skills" / PACKAGED_SKILL_NAME
    (dest / "SKILL.md").write_text("tampered\n", encoding="utf-8")

    status = CliRunner().invoke(
        package_group,
        ["status", "--workdir", str(workdir), "--home", str(tmp_path / "home")],
    )
    assert status.exit_code == 2, status.output


def test_status_json_lists_installs(tmp_path: Path) -> None:
    import json

    workdir = _workdir(tmp_path)
    CliRunner().invoke(
        package_group,
        ["install", "--host", "claude", "--scope", "project", "--workdir", str(workdir)],
    )
    status = CliRunner().invoke(
        package_group,
        ["status", "--json", "--workdir", str(workdir), "--home", str(tmp_path / "home")],
    )
    assert status.exit_code == 0, status.output
    payload = json.loads(status.output)
    assert any(row["host"] == "claude" and row["verified"] for row in payload["installs"])


def test_status_no_installs_exits_0(tmp_path: Path) -> None:
    workdir = _workdir(tmp_path)
    status = CliRunner().invoke(
        package_group,
        ["status", "--workdir", str(workdir), "--home", str(tmp_path / "home")],
    )
    assert status.exit_code == 0, status.output


# ---------------------------------------------------------------------------
# conformance (multi-host live validation, issue #2369 tail)
# ---------------------------------------------------------------------------


class _InProcessTransport:
    """Faithful transport: run the real root CLI in-process via ``CliRunner``."""

    def invoke(self, host, argv, *, cwd):
        from bernstein.cli.main import cli
        from bernstein.core.skills.conformance import CommandResult

        result = CliRunner().invoke(cli, list(argv))
        return CommandResult(argv=tuple(argv), exit_code=result.exit_code)


class _RedTransport:
    """Return exit 2 for one named host, 0 otherwise."""

    def __init__(self, red_host):
        self._red = red_host

    def invoke(self, host, argv, *, cwd):
        from bernstein.core.skills.conformance import CommandResult

        return CommandResult(argv=tuple(argv), exit_code=2 if host == self._red else 0)


def _patch_transport(monkeypatch, transport):
    monkeypatch.setattr(
        "bernstein.cli.commands.skills_package_cmd._default_transport",
        lambda: transport,
    )


def test_conformance_three_hosts_pass_exit_0(monkeypatch, tmp_path: Path) -> None:
    workdir = _workdir(tmp_path)
    _patch_transport(monkeypatch, _InProcessTransport())
    result = CliRunner().invoke(
        package_group,
        [
            "conformance",
            "--host",
            "claude",
            "--host",
            "codex",
            "--host",
            "cursor",
            "--min-hosts",
            "3",
            "--workdir",
            str(workdir),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "PASS" in result.output


def test_conformance_json_shape(monkeypatch, tmp_path: Path) -> None:
    import json

    workdir = _workdir(tmp_path)
    _patch_transport(monkeypatch, _InProcessTransport())
    result = CliRunner().invoke(
        package_group,
        [
            "conformance",
            "--host",
            "claude",
            "--host",
            "codex",
            "--host",
            "cursor",
            "--json",
            "--workdir",
            str(workdir),
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert set(payload["passed_hosts"]) == {"claude", "codex", "cursor"}
    assert payload["receipt_id"].startswith("sha256:")


def test_conformance_red_host_exit_2(monkeypatch, tmp_path: Path) -> None:
    workdir = _workdir(tmp_path)
    _patch_transport(monkeypatch, _RedTransport("cursor"))
    result = CliRunner().invoke(
        package_group,
        [
            "conformance",
            "--host",
            "claude",
            "--host",
            "codex",
            "--host",
            "cursor",
            "--workdir",
            str(workdir),
        ],
    )
    assert result.exit_code == 2, result.output
    assert "FAILED" in result.output


def test_conformance_below_min_hosts_exit_2(monkeypatch, tmp_path: Path) -> None:
    workdir = _workdir(tmp_path)
    _patch_transport(monkeypatch, _InProcessTransport())
    result = CliRunner().invoke(
        package_group,
        ["conformance", "--host", "claude", "--host", "codex", "--min-hosts", "3", "--workdir", str(workdir)],
    )
    assert result.exit_code == 2, result.output


def _image_repo(
    root: Path, *, oci_tag: str = "3.4.1", catalog_image: str = "ghcr.io/sipyourdrink-ltd/bernstein"
) -> Path:
    """Write a minimal server.json + docker-mcp catalog for image-verify tests."""
    import json

    (root / "server.json").write_text(
        json.dumps(
            {
                "name": "io.github.sipyourdrink-ltd/bernstein",
                "repository": {"url": "https://github.com/sipyourdrink-ltd/bernstein", "source": "github"},
                "version": "3.4.1",
                "packages": [
                    {"registryType": "pypi", "identifier": "bernstein", "version": "3.4.1"},
                    {"registryType": "oci", "identifier": f"ghcr.io/sipyourdrink-ltd/bernstein:{oci_tag}"},
                ],
            }
        ),
        encoding="utf-8",
    )
    catalog = root / "packaging" / "docker-mcp"
    catalog.mkdir(parents=True)
    (catalog / "server.yaml").write_text(f"name: bernstein\nimage: {catalog_image}\n", encoding="utf-8")
    return root


def test_image_verify_ok_exit_0(tmp_path: Path) -> None:
    _image_repo(tmp_path)
    result = CliRunner().invoke(
        package_group,
        ["image-verify", "--version", "3.4.1", "--repo-root", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert "OK" in result.output
    assert "ghcr.io/sipyourdrink-ltd/bernstein:3.4.1" in result.output


def test_image_verify_json_shape(tmp_path: Path) -> None:
    import json

    _image_repo(tmp_path)
    result = CliRunner().invoke(
        package_group,
        ["image-verify", "--version", "3.4.1", "--repo-root", str(tmp_path), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["provenance"]["ok"] is True
    assert payload["version"] == "3.4.1"


def test_image_verify_mismatch_exit_2(tmp_path: Path) -> None:
    _image_repo(tmp_path, catalog_image="ghcr.io/other/bernstein")
    result = CliRunner().invoke(
        package_group,
        ["image-verify", "--version", "3.4.1", "--repo-root", str(tmp_path)],
    )
    assert result.exit_code == 2, result.output
    assert "FAILED" in result.output
