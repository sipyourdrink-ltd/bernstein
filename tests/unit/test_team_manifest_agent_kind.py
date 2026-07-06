"""Team-manifest ``agent_kind`` declaration for the activity boundary (#2311).

The typed activity boundary lets any agent modality run under the deterministic
scheduler; a team declares which modality each role runs as via an optional
``agent_kind`` key on the ``[[roles]]`` entry. It defaults to ``coding`` (the
modality the scheduler is already validated for), so every existing manifest
keeps a byte-identical canonical form and an unchanged digest.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.orchestration.activity import ActivityKind
from bernstein.core.teams.manifest import (
    TeamManifestValidationError,
    TeamRoleSpec,
    canonical_toml,
    load_team_manifest,
)

_WITH_KINDS = """\
name = "research-crew"
version = "1.0.0"

[coordination]
parallelism = 2
review_chain = false

[[roles]]
agent_kind = "research"
role = "scout"

[[roles]]
role = "backend"
"""

_LEGACY = """\
name = "crew"
version = "1.0.0"

[coordination]
parallelism = 1
review_chain = false

[[roles]]
role = "backend"
"""


def _write(tmp_path: Path, name: str, body: str) -> Path:
    teams_dir = tmp_path / "templates" / "teams"
    teams_dir.mkdir(parents=True, exist_ok=True)
    path = teams_dir / f"{name}.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_role_agent_kind_parses(tmp_path: Path) -> None:
    manifest = load_team_manifest(_write(tmp_path, "research-crew", _WITH_KINDS))
    by_role = {r.role: r for r in manifest.roles}
    assert by_role["scout"].agent_kind is ActivityKind.RESEARCH
    # A role without the key defaults to the coding modality.
    assert by_role["backend"].agent_kind is ActivityKind.CODING


def test_agent_kind_defaults_to_coding_when_omitted(tmp_path: Path) -> None:
    manifest = load_team_manifest(_write(tmp_path, "crew", _LEGACY))
    assert manifest.roles[0].agent_kind is ActivityKind.CODING


def test_unknown_agent_kind_is_rejected(tmp_path: Path) -> None:
    bad = _LEGACY.replace('role = "backend"', 'agent_kind = "telepathy"\nrole = "backend"')
    with pytest.raises(TeamManifestValidationError, match="agent_kind"):
        load_team_manifest(_write(tmp_path, "bad", bad))


def test_canonical_toml_emits_non_default_agent_kind(tmp_path: Path) -> None:
    manifest = load_team_manifest(_write(tmp_path, "research-crew", _WITH_KINDS))
    rendered = canonical_toml(manifest)
    assert 'agent_kind = "research"' in rendered
    # The default (coding) role does not emit the key, keeping legacy manifests
    # byte-identical.
    assert rendered.count("agent_kind") == 1


def test_canonical_toml_omits_default_agent_kind(tmp_path: Path) -> None:
    manifest = load_team_manifest(_write(tmp_path, "crew", _LEGACY))
    assert "agent_kind" not in canonical_toml(manifest)


def test_legacy_manifest_digest_is_unchanged_by_new_field(tmp_path: Path) -> None:
    # A manifest with no agent_kind must serialize byte-identically to its
    # on-disk legacy form, so its digest does not shift under the new field.
    manifest = load_team_manifest(_write(tmp_path, "crew", _LEGACY))
    assert canonical_toml(manifest) == _LEGACY


def test_agent_kind_round_trips_as_fixpoint(tmp_path: Path) -> None:
    manifest = load_team_manifest(_write(tmp_path, "research-crew", _WITH_KINDS))
    rendered = canonical_toml(manifest)
    reloaded = load_team_manifest(_write(tmp_path, "roundtrip", rendered))
    assert canonical_toml(reloaded) == rendered
    assert reloaded.digest() == manifest.digest()


def test_team_role_spec_default_kind_is_coding() -> None:
    assert TeamRoleSpec(role="qa").agent_kind is ActivityKind.CODING
