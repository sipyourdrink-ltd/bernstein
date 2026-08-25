"""A plugin must not silently replace a skill that came from elsewhere.

Covers the cross-source guard in ``install_plugin_local``
(``src/bernstein/core/skills/lifecycle.py``). Before it, ``install_local`` +
``_record_plugin_lock`` overwrote a same-named skill's install directory and
lock row regardless of the prior row's ``source``: installing a pack
containing ``alpha`` replaced an ``alpha`` from ``bernstein-skills.toml`` or
another plugin with no warning, no refusal, and the row's provenance flipped
to ``"plugin"``.

That is a shadowing path, not just a tidiness problem - the operator chose the
skill they installed, and nothing told them it was gone.

The refusal has to bite *before* ``install_local`` runs, because that call
already clobbers the target directory. A guard that only skipped the lock
write would leave the previous skill's tree destroyed and unrecorded, which is
strictly worse than the bug it replaced. ``test_refused_collision_leaves_the_
previous_install_untouched`` is the assertion that pins that ordering.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from bernstein.core.skills.lifecycle import (
    SKILLS_LOCK_FILENAME,
    InstallScope,
    LockEntry,
    _read_lock,
    _write_lock,
    install_plugin_local,
    scope_root,
)

TOML_SOURCE = "bernstein-skills.toml"


def _write_skill(path: Path, name: str, body: str = "Body content.") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        textwrap.dedent(
            f"""\
            ---
            name: {name}
            description: Plugin skill for tests.
            ---

            # {name}

            {body}
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )


@pytest.fixture
def plugin_dir(tmp_path: Path) -> Path:
    root = tmp_path / "my-pack"
    skills = root / "skills"
    skills.mkdir(parents=True)
    (root / "plugin.json").write_text(
        json.dumps({"name": "my-pack", "version": "1.0.0", "skills": "./skills/"}),
        encoding="utf-8",
    )
    for name in ("alpha", "beta"):
        _write_skill(skills / name / "SKILL.md", name, body=f"From the plugin: {name}.")
    return root


@pytest.fixture
def workdir_with_toml_alpha(tmp_path: Path) -> Path:
    """A project whose lock already claims ``alpha`` from the TOML source."""
    workdir = tmp_path / "project"
    workdir.mkdir()
    dest = scope_root(InstallScope.PROJECT, workdir=workdir) / "alpha"
    _write_skill(dest / "SKILL.md", "alpha", body="From bernstein-skills.toml.")
    _write_lock(
        workdir / SKILLS_LOCK_FILENAME,
        [LockEntry(name="alpha", source=TOML_SOURCE, path=str(dest), digest="sha256:deadbeef")],
    )
    return workdir


def test_cross_source_collision_is_refused(plugin_dir: Path, workdir_with_toml_alpha: Path) -> None:
    result = install_plugin_local(plugin_dir, scope=InstallScope.PROJECT, workdir=workdir_with_toml_alpha)

    assert [s.name for s in result.skipped] == ["alpha"]
    assert TOML_SOURCE in result.skipped[0].reason
    assert "--force" in result.skipped[0].reason


def test_non_colliding_skills_still_install(plugin_dir: Path, workdir_with_toml_alpha: Path) -> None:
    """One refusal must not abort the pack - the per-skill contract holds."""
    result = install_plugin_local(plugin_dir, scope=InstallScope.PROJECT, workdir=workdir_with_toml_alpha)

    assert [r.name for r in result.installed] == ["beta"]


def test_refused_collision_leaves_the_previous_install_untouched(
    plugin_dir: Path, workdir_with_toml_alpha: Path
) -> None:
    """The refusal fires before install_local writes, so nothing is clobbered."""
    dest = scope_root(InstallScope.PROJECT, workdir=workdir_with_toml_alpha) / "alpha" / "SKILL.md"
    before = dest.read_text(encoding="utf-8")

    install_plugin_local(plugin_dir, scope=InstallScope.PROJECT, workdir=workdir_with_toml_alpha)

    assert dest.read_text(encoding="utf-8") == before
    assert "bernstein-skills.toml" in dest.read_text(encoding="utf-8")


def test_refused_collision_does_not_flip_lock_provenance(plugin_dir: Path, workdir_with_toml_alpha: Path) -> None:
    install_plugin_local(plugin_dir, scope=InstallScope.PROJECT, workdir=workdir_with_toml_alpha)

    entries = _read_lock(workdir_with_toml_alpha / SKILLS_LOCK_FILENAME)
    assert entries["alpha"].source == TOML_SOURCE
    assert entries["alpha"].digest == "sha256:deadbeef"


def test_force_takes_the_plugin_copy(plugin_dir: Path, workdir_with_toml_alpha: Path) -> None:
    result = install_plugin_local(plugin_dir, scope=InstallScope.PROJECT, workdir=workdir_with_toml_alpha, force=True)

    assert {r.name for r in result.installed} == {"alpha", "beta"}
    assert result.skipped == []
    entries = _read_lock(workdir_with_toml_alpha / SKILLS_LOCK_FILENAME)
    assert entries["alpha"].source == "plugin"
    dest = scope_root(InstallScope.PROJECT, workdir=workdir_with_toml_alpha) / "alpha" / "SKILL.md"
    assert "From the plugin" in dest.read_text(encoding="utf-8")


def test_same_source_reinstall_stays_silent(plugin_dir: Path, tmp_path: Path) -> None:
    """Drift heal and upgrade are unchanged: no refusal, no notice."""
    workdir = tmp_path / "project"

    first = install_plugin_local(plugin_dir, scope=InstallScope.PROJECT, workdir=workdir)
    second = install_plugin_local(plugin_dir, scope=InstallScope.PROJECT, workdir=workdir)

    assert {r.name for r in first.installed} == {"alpha", "beta"}
    assert {r.name for r in second.installed} == {"alpha", "beta"}
    assert second.skipped == []


def test_collision_with_another_plugin_source_is_allowed(plugin_dir: Path, tmp_path: Path) -> None:
    """Pack-over-pack keeps today's behaviour; only cross-*source* is refused.

    Both rows carry ``source="plugin"``, so this is the same-source path. The
    issue scopes namespacing skills by *pack* out, so a second pack shadowing
    the first stays as it was rather than being half-fixed here.
    """
    workdir = tmp_path / "project"
    install_plugin_local(plugin_dir, scope=InstallScope.PROJECT, workdir=workdir)

    other = tmp_path / "other-pack"
    (other / "skills").mkdir(parents=True)
    (other / "plugin.json").write_text(
        json.dumps({"name": "other-pack", "version": "1.0.0", "skills": "./skills/"}),
        encoding="utf-8",
    )
    _write_skill(other / "skills" / "alpha" / "SKILL.md", "alpha", body="From other-pack.")

    result = install_plugin_local(other, scope=InstallScope.PROJECT, workdir=workdir)

    assert [r.name for r in result.installed] == ["alpha"]
    assert result.skipped == []
