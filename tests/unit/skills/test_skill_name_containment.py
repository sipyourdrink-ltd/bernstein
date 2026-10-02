"""A skill name can never place, replace or delete anything outside the skills root.

Two layers own this:

* the catalog parser rejects an entry ``name`` that is not a skill slug, the
  same rule ``SKILL.md`` names follow, since that name becomes the install
  directory;
* the lifecycle functions that write or delete under the skills root resolve
  the target and refuse it unless it is a direct child of that root, for
  names that reach them from any other path (lock files, the CLI).
"""

from __future__ import annotations

import copy
import textwrap
from pathlib import Path
from typing import Any

import pytest

from bernstein.core.skills.catalog import SkillCatalogValidationError, validate_catalog
from bernstein.core.skills.lifecycle import (
    InstallScope,
    SkillLifecycleError,
    install_local,
    remove_skill,
    scope_root,
)

_BASE_PAYLOAD: dict[str, Any] = {
    "version": 1,
    "generated_at": "2026-05-21T00:00:00Z",
    "entries": [
        {
            "id": "code-review",
            "name": "code-review",
            "version": "1.0.0",
            "description": "Review code.",
            "source": {"kind": "github", "repo": "acme/code-review", "tag": "v1.0.0"},
            "content_digest": "f" * 64,
        }
    ],
}

_UNSAFE_NAMES = [
    "../escape",
    "../../..",
    "nested/child",
    "/etc",
    "..",
    ".",
    "back\\slash",
    "Upper",
    "trailing\n",
    "",
]


def _payload_with_name(name: str) -> dict[str, Any]:
    payload = copy.deepcopy(_BASE_PAYLOAD)
    payload["entries"][0]["name"] = name
    return payload


# ---------------------------------------------------------------------------
# Catalog parse time
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", _UNSAFE_NAMES)
def test_catalog_rejects_unsafe_entry_name(name: str) -> None:
    with pytest.raises(SkillCatalogValidationError, match="name"):
        validate_catalog(_payload_with_name(name))


def test_catalog_accepts_slug_entry_name() -> None:
    catalog = validate_catalog(_payload_with_name("code-review-2"))
    assert catalog.entries[0].name == "code-review-2"


def test_catalog_id_rejects_trailing_newline() -> None:
    payload = copy.deepcopy(_BASE_PAYLOAD)
    payload["entries"][0]["id"] = "code-review\n"
    with pytest.raises(SkillCatalogValidationError, match="id"):
        validate_catalog(payload)


# ---------------------------------------------------------------------------
# Lifecycle write / delete sites
# ---------------------------------------------------------------------------


@pytest.fixture
def skill_source(tmp_path: Path) -> Path:
    source = tmp_path / "src-skill"
    source.mkdir()
    (source / "SKILL.md").write_text(
        textwrap.dedent(
            """
            ---
            name: src-skill
            description: Skill used to check install containment under the skills root.
            ---

            # Skill
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    return source


@pytest.mark.parametrize("name", ["../victim", "../../victim", "a/../../victim", "..", "."])
def test_install_local_refuses_override_name_outside_root(tmp_path: Path, skill_source: Path, name: str) -> None:
    workdir = tmp_path / "proj"
    workdir.mkdir()
    root = scope_root(InstallScope.PROJECT, workdir=workdir)
    # Something the operator owns, next to and above the skills root.
    victim = root.parent / "victim"
    victim.mkdir(parents=True)
    (victim / "keep.txt").write_text("keep", encoding="utf-8")
    above = workdir / ".bernstein" / "victim"
    above.mkdir(parents=True, exist_ok=True)
    (above / "keep.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(SkillLifecycleError, match="outside"):
        install_local(skill_source, scope=InstallScope.PROJECT, workdir=workdir, override_name=name)

    assert (victim / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert (above / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert root.parent.is_dir()


def test_install_local_refuses_symlinked_install_dir(tmp_path: Path, skill_source: Path) -> None:
    workdir = tmp_path / "proj"
    workdir.mkdir()
    root = scope_root(InstallScope.PROJECT, workdir=workdir)
    root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    (root / "src-skill").symlink_to(outside)

    with pytest.raises(SkillLifecycleError, match="outside"):
        install_local(skill_source, scope=InstallScope.PROJECT, workdir=workdir)

    assert (outside / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_install_local_still_installs_a_plain_name(tmp_path: Path, skill_source: Path) -> None:
    workdir = tmp_path / "proj"
    workdir.mkdir()

    result = install_local(skill_source, scope=InstallScope.PROJECT, workdir=workdir, override_name="renamed")

    assert result.install_dir == scope_root(InstallScope.PROJECT, workdir=workdir) / "renamed"
    assert (result.install_dir / "SKILL.md").is_file()


@pytest.mark.parametrize("name", ["..", "../victim", "a/../../victim"])
def test_remove_skill_refuses_name_outside_root(tmp_path: Path, name: str) -> None:
    workdir = tmp_path / "proj"
    root = scope_root(InstallScope.PROJECT, workdir=workdir)
    root.mkdir(parents=True)
    victim = root.parent / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(SkillLifecycleError, match="outside"):
        remove_skill(name, scope=InstallScope.PROJECT, workdir=workdir)

    assert (victim / "keep.txt").is_file()
    assert root.is_dir()
