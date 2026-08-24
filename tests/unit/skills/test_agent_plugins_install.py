"""Tests for Agent Plugins directory layout recognition in skills install (#3772).

Slice 1 of #3540: ``bernstein skills install <path>`` must detect an Agent
Plugins v1.0.0 directory layout (root ``plugin.json`` + ``skills/<name>/SKILL.md``
tree) and install every conformant skill in a single invocation.

Design decision (strict layout detection): a directory counts as an Agent
Plugins layout only when a root ``plugin.json`` parses with a ``name`` field
and a ``skills`` field that resolves to a ``skills/`` subdirectory. Rationale:
the strict check avoids misfiring on unrelated directories that merely happen
to contain a ``skills/`` folder (e.g. a developer's checkout), which a looser
"any ``skills/*/SKILL.md`` tree" heuristic would install by surprise.

Red-green note: this file references ``install_plugin_local`` and
``is_agent_plugins_layout`` which do not exist yet — that is the point.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from bernstein.core.skills.lifecycle import (
    InstallScope,
    SkillLifecycleError,
    install_plugin_local,
    is_agent_plugins_layout,
    scope_root,
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
        json.dumps({"name": name, "version": "1.0.0", "skills": skills}),
        encoding="utf-8",
    )


@pytest.fixture
def plugin_dir(tmp_path: Path) -> Path:
    """A conformant Agent Plugins directory with three valid skills."""
    root = tmp_path / "my-pack"
    skills = root / "skills"
    skills.mkdir(parents=True)
    _write_manifest(root / "plugin.json", name="my-pack")
    for name in ("alpha", "beta", "gamma"):
        _write_skill(skills / name / "SKILL.md", name)
    return root


# ---------------------------------------------------------------------------
# Layout detection (strict mode)
# ---------------------------------------------------------------------------


def test_layout_detection_accepts_conformant_plugin_dir(plugin_dir: Path) -> None:
    assert is_agent_plugins_layout(plugin_dir) is True


def test_layout_detection_rejects_directory_without_manifest(tmp_path: Path) -> None:
    """A directory with skills/ but no plugin.json is NOT a plugin layout."""
    root = tmp_path / "no-manifest"
    (root / "skills" / "alpha").mkdir(parents=True)
    _write_skill(root / "skills" / "alpha" / "SKILL.md", "alpha")
    assert is_agent_plugins_layout(root) is False


def test_layout_detection_rejects_manifest_without_name_field(tmp_path: Path) -> None:
    root = tmp_path / "no-name"
    (root / "skills" / "alpha").mkdir(parents=True)
    _write_skill(root / "skills" / "alpha" / "SKILL.md", "alpha")
    (root / "plugin.json").write_text(
        json.dumps({"version": "1.0.0", "skills": "./skills/"}),
        encoding="utf-8",
    )
    assert is_agent_plugins_layout(root) is False


def test_layout_detection_rejects_invalid_manifest_json(tmp_path: Path) -> None:
    root = tmp_path / "bad-json"
    (root / "skills" / "alpha").mkdir(parents=True)
    _write_skill(root / "skills" / "alpha" / "SKILL.md", "alpha")
    (root / "plugin.json").write_text("not json {", encoding="utf-8")
    assert is_agent_plugins_layout(root) is False


# ---------------------------------------------------------------------------
# Installation behaviour
# ---------------------------------------------------------------------------


def test_install_plugin_installs_all_three_skills(
    plugin_dir: Path,
    tmp_path: Path,
) -> None:
    workdir = tmp_path / "project"
    result = install_plugin_local(
        plugin_dir,
        scope=InstallScope.PROJECT,
        workdir=workdir,
    )

    assert {r.name for r in result.installed} == {"alpha", "beta", "gamma"}
    assert result.skipped == []
    dest = scope_root(InstallScope.PROJECT, workdir=workdir)
    for name in ("alpha", "beta", "gamma"):
        assert (dest / name / "SKILL.md").is_file()
    # Every installed skill carries a content digest (lockfile material).
    assert all(r.digest.digest for r in result.installed)


def test_install_plugin_skips_malformed_skill_and_reports_name(
    plugin_dir: Path,
    tmp_path: Path,
) -> None:
    # Corrupt one skill: missing frontmatter entirely.
    (plugin_dir / "skills" / "beta" / "SKILL.md").write_text(
        "# beta\n\nno frontmatter here\n",
        encoding="utf-8",
    )

    workdir = tmp_path / "project"
    result = install_plugin_local(
        plugin_dir,
        scope=InstallScope.PROJECT,
        workdir=workdir,
    )

    assert {r.name for r in result.installed} == {"alpha", "gamma"}
    assert len(result.skipped) == 1
    assert result.skipped[0].name == "beta"


def test_install_plugin_rejects_non_plugin_directory(
    tmp_path: Path,
) -> None:
    """A plain skill directory (no plugin.json) is not a plugin install target."""
    plain = tmp_path / "plain-skill"
    plain.mkdir()
    _write_skill(plain / "SKILL.md", "plain-skill")

    with pytest.raises(SkillLifecycleError):
        install_plugin_local(
            plain,
            scope=InstallScope.PROJECT,
            workdir=tmp_path / "project",
        )


def test_install_plugin_writes_lockfile_entries(
    plugin_dir: Path,
    tmp_path: Path,
) -> None:
    """Every installed skill lands in skills.lock with a content digest."""
    workdir = tmp_path / "project"
    install_plugin_local(
        plugin_dir,
        scope=InstallScope.PROJECT,
        workdir=workdir,
    )

    lock_path = workdir / "skills.lock"
    assert lock_path.is_file()
    content = lock_path.read_text(encoding="utf-8")
    resolved_root = plugin_dir.resolve()
    for name in ("alpha", "beta", "gamma"):
        assert f'name = "{name}"' in content
        # path names the skills/<name>/ directory the skill came from, not
        # the plugin root, so sync/drift can locate the individual tree.
        assert f'path = "{resolved_root}/skills/{name}"' in content


# ---------------------------------------------------------------------------
# Manifest containment (#4448 review): an untrusted plugin.json must not be
# able to make detection or install walk outside the plugin root.
# ---------------------------------------------------------------------------


def test_layout_detection_rejects_manifest_with_escaping_skills(tmp_path: Path) -> None:
    """A manifest whose skills field escapes the plugin root is NOT a plugin layout."""
    root = tmp_path / "escaping"
    (root / "skills" / "alpha").mkdir(parents=True)
    _write_skill(root / "skills" / "alpha" / "SKILL.md", "alpha")
    _write_manifest(root / "plugin.json", name="escaping", skills="../../outside")
    assert is_agent_plugins_layout(root) is False


def test_layout_detection_rejects_manifest_with_absolute_skills(tmp_path: Path) -> None:
    """An absolute skills value short-circuits the join and is refused."""
    root = tmp_path / "absolute"
    (root / "skills" / "alpha").mkdir(parents=True)
    _write_skill(root / "skills" / "alpha" / "SKILL.md", "alpha")
    _write_manifest(root / "plugin.json", name="absolute", skills=str(tmp_path))
    assert is_agent_plugins_layout(root) is False


def test_layout_detection_rejects_symlinked_skills_escape(tmp_path: Path) -> None:
    """A skills directory that is a symlink out of the tree is refused."""
    root = tmp_path / "symlink-escape"
    root.mkdir()
    outside = tmp_path / "outside"
    (outside / "alpha").mkdir(parents=True)
    _write_skill(outside / "alpha" / "SKILL.md", "alpha")
    (root / "skills").symlink_to(outside, target_is_directory=True)
    _write_manifest(root / "plugin.json", name="symlink-escape")
    assert is_agent_plugins_layout(root) is False


def test_install_plugin_raises_on_escaping_manifest(tmp_path: Path) -> None:
    """install_plugin_local refuses an escaping skills field outright.

    Nothing may be installed into scope from outside the plugin root, and
    the failure is a hard error rather than a silent empty install.
    """
    root = tmp_path / "escaping"
    (root / "skills" / "alpha").mkdir(parents=True)
    _write_skill(root / "skills" / "alpha" / "SKILL.md", "alpha")
    _write_manifest(root / "plugin.json", name="escaping", skills="../../outside")

    workdir = tmp_path / "project"
    with pytest.raises(SkillLifecycleError):
        install_plugin_local(
            root,
            scope=InstallScope.PROJECT,
            workdir=workdir,
        )

    # Nothing outside the root was walked or copied into scope.
    dest = scope_root(InstallScope.PROJECT, workdir=workdir)
    assert not dest.exists()
