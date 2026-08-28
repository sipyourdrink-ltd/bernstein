"""Unit tests for the team-hub schema + convention loader.

Covers the smallest viable surface:

- :class:`TeamHubManifest` accepts a well-formed manifest dict
- traversal / absolute-path entries are rejected at schema time
- :func:`parse_team_hub_yaml` round-trips a real on-disk fixture
- :func:`load_team_hub` returns ``None`` when the convention dirs are absent
- :func:`load_team_hub` resolves declared paths and orders them
  ``agents → skills → rules``
- a manifest entry pointing at a missing path raises
  :class:`TeamHubLoaderError`
"""

from __future__ import annotations

from pathlib import Path

import pytest
from bernstein.core.plugins_core.team_hub_loader import (
    LoadedTeamHub,
    TeamHubLoaderError,
    load_team_hub,
)

from bernstein.core.plugins_core.team_hub_manifest import (
    TeamHubManifest,
    TeamHubManifestError,
    parse_team_hub_yaml,
    validate_team_hub_dict,
)


def _good_dict() -> dict[str, object]:
    """Minimal valid manifest dict reused by several positive-path tests."""
    return {
        "name": "acmecorp-shared",
        "version": "1.0",
        "ships": {
            "agents": ["team/agents/reviewer/"],
            "skills": ["team/skills/deploy-prod/"],
            "rules": ["team/rules/no-print.md"],
        },
        "compatibility": {"bernstein": ">=1.10"},
    }


def _write_hub(root: Path, *, manifest_yaml: str) -> Path:
    """Materialise a hub fixture at ``root`` and return the directory."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "team-hub.yaml").write_text(manifest_yaml, encoding="utf-8")
    (root / "team" / "agents" / "reviewer").mkdir(parents=True, exist_ok=True)
    (root / "team" / "agents" / "reviewer" / "AGENT.md").write_text("# Reviewer agent\n", encoding="utf-8")
    (root / "team" / "skills" / "deploy-prod").mkdir(parents=True, exist_ok=True)
    (root / "team" / "skills" / "deploy-prod" / "SKILL.md").write_text(
        "---\nname: deploy-prod\n---\n", encoding="utf-8"
    )
    (root / "team" / "rules").mkdir(parents=True, exist_ok=True)
    (root / "team" / "rules" / "no-print.md").write_text("# Rule\nNo print statements.\n", encoding="utf-8")
    return root


# ---------------------------------------------------------------- schema --


def test_validate_team_hub_dict_accepts_minimal_manifest() -> None:
    """A canonical dict round-trips into a fully populated manifest."""
    manifest = validate_team_hub_dict(_good_dict())

    assert isinstance(manifest, TeamHubManifest)
    assert manifest.name == "acmecorp-shared"
    assert manifest.version == "1.0"
    # Trailing slash on the input is normalised away.
    assert manifest.ships.agents == ["team/agents/reviewer"]
    assert manifest.ships.skills == ["team/skills/deploy-prod"]
    assert manifest.ships.rules == ["team/rules/no-print.md"]
    assert manifest.compatibility.bernstein == ">=1.10"


def test_validate_team_hub_dict_rejects_bad_name() -> None:
    """``name`` must be a lowercase slug - uppercase fails fast."""
    bad = _good_dict()
    bad["name"] = "AcmeCorp"

    with pytest.raises(TeamHubManifestError) as exc:
        validate_team_hub_dict(bad)

    assert "must match regex" in exc.value.detail


def test_validate_team_hub_dict_rejects_traversal_entry() -> None:
    """``..`` segments cannot smuggle a path outside the hub root."""
    bad = _good_dict()
    ships = bad["ships"]
    assert isinstance(ships, dict)
    ships["agents"] = ["../../../etc/passwd"]

    with pytest.raises(TeamHubManifestError):
        validate_team_hub_dict(bad)


def test_validate_team_hub_dict_rejects_absolute_entry() -> None:
    """Absolute paths cannot ride in via the schema."""
    bad = _good_dict()
    ships = bad["ships"]
    assert isinstance(ships, dict)
    ships["skills"] = ["/etc/skills/secret"]

    with pytest.raises(TeamHubManifestError):
        validate_team_hub_dict(bad)


def test_validate_team_hub_dict_rejects_unknown_bucket() -> None:
    """Unknown shipping bucket surfaces a friendly, allowlisted error."""
    bad = _good_dict()
    ships = bad["ships"]
    assert isinstance(ships, dict)
    ships["plugins"] = ["team/plugins/foo"]

    with pytest.raises(TeamHubManifestError) as exc:
        validate_team_hub_dict(bad)

    assert "unknown ships bucket" in exc.value.detail


def test_validate_team_hub_dict_rejects_extra_top_level_keys() -> None:
    """Pydantic's ``extra='forbid'`` catches typos at the top level too."""
    bad = _good_dict()
    bad["scope"] = "private"

    with pytest.raises(TeamHubManifestError):
        validate_team_hub_dict(bad)


# -------------------------------------------------------------- yaml file --


def test_parse_team_hub_yaml_round_trip(tmp_path: Path) -> None:
    """A real on-disk manifest parses identically to the dict form."""
    manifest_text = (
        "name: acmecorp-shared\n"
        "version: '1.0'\n"
        "ships:\n"
        "  agents:\n"
        "    - team/agents/reviewer/\n"
        "  skills: []\n"
        "  rules: []\n"
        "compatibility:\n"
        "  bernstein: '>=1.10'\n"
    )
    path = tmp_path / "team-hub.yaml"
    path.write_text(manifest_text, encoding="utf-8")

    manifest = parse_team_hub_yaml(path)

    assert manifest.name == "acmecorp-shared"
    assert manifest.ships.agents == ["team/agents/reviewer"]
    assert manifest.ships.skills == []
    assert manifest.compatibility.bernstein == ">=1.10"


def test_parse_team_hub_yaml_missing_file_raises(tmp_path: Path) -> None:
    """Missing file is a hard error - the loader uses ``is_file()`` first."""
    with pytest.raises(TeamHubManifestError) as exc:
        parse_team_hub_yaml(tmp_path / "team-hub.yaml")

    assert "does not exist" in exc.value.detail


def test_parse_team_hub_yaml_rejects_oversize(tmp_path: Path) -> None:
    """Manifests larger than the cap are rejected before YAML parsing."""
    path = tmp_path / "team-hub.yaml"
    # 64 KiB cap + a single byte over it - YAML noise is fine.
    path.write_text("# " + ("a" * (64 * 1024)) + "\nname: x\n", encoding="utf-8")

    with pytest.raises(TeamHubManifestError) as exc:
        parse_team_hub_yaml(path)

    assert "exceeds" in exc.value.detail


# --------------------------------------------------------------- loader ----


def test_load_team_hub_missing_root_is_noop(tmp_path: Path) -> None:
    """A path that doesn't exist returns ``None`` - no exception."""
    assert load_team_hub(tmp_path / "does-not-exist") is None


def test_load_team_hub_missing_manifest_is_noop(tmp_path: Path) -> None:
    """An empty directory returns ``None`` - operator hasn't initialised yet."""
    (tmp_path / "team").mkdir()
    assert load_team_hub(tmp_path) is None


def test_load_team_hub_missing_team_dir_is_noop(tmp_path: Path) -> None:
    """Manifest without the ``team/`` convention dir is treated as not-yet-populated."""
    (tmp_path / "team-hub.yaml").write_text(
        "name: x\nversion: '1'\ncompatibility:\n  bernstein: '>=1'\n",
        encoding="utf-8",
    )
    assert load_team_hub(tmp_path) is None


def test_load_team_hub_resolves_entries(tmp_path: Path) -> None:
    """A populated hub yields entries in the canonical ``agents → skills → rules`` order."""
    manifest_text = (
        "name: acmecorp-shared\n"
        "version: '1.0'\n"
        "ships:\n"
        "  agents:\n"
        "    - team/agents/reviewer/\n"
        "  skills:\n"
        "    - team/skills/deploy-prod/\n"
        "  rules:\n"
        "    - team/rules/no-print.md\n"
        "compatibility:\n"
        "  bernstein: '>=1.10'\n"
    )
    hub = _write_hub(tmp_path / "hub", manifest_yaml=manifest_text)

    loaded = load_team_hub(hub)

    assert isinstance(loaded, LoadedTeamHub)
    assert [(e.bucket, e.relative, e.is_directory) for e in loaded.entries] == [
        ("agents", "team/agents/reviewer", True),
        ("skills", "team/skills/deploy-prod", True),
        ("rules", "team/rules/no-print.md", False),
    ]
    # ``by_bucket`` filters but preserves source order.
    assert loaded.by_bucket("agents") == (loaded.entries[0],)
    assert loaded.by_bucket("rules") == (loaded.entries[2],)
    # All resolved paths live inside the hub root.
    real_root = hub.resolve()
    for entry in loaded.entries:
        assert entry.absolute.resolve().is_relative_to(real_root)


def test_load_team_hub_missing_entry_raises(tmp_path: Path) -> None:
    """Manifest claims a file that isn't on disk → :class:`TeamHubLoaderError`."""
    manifest_text = (
        "name: acmecorp-shared\n"
        "version: '1.0'\n"
        "ships:\n"
        "  rules:\n"
        "    - team/rules/missing.md\n"
        "compatibility:\n"
        "  bernstein: '>=1.10'\n"
    )
    hub = _write_hub(tmp_path / "hub", manifest_yaml=manifest_text)
    # Remove the file the schema check can't catch - the path is structurally valid.
    (hub / "team" / "rules" / "no-print.md").unlink()

    with pytest.raises(TeamHubLoaderError) as exc:
        load_team_hub(hub)

    assert "missing path" in exc.value.detail
