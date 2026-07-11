"""Seed parser tests for ``team_manifest:`` expansion (issue #2248).

Covers acceptance criteria 1 (pure, byte-identical expansion into the
existing team + policy structures) and 4 (a mismatched ``name@sha256``
pin refuses to run with a typed error).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from bernstein.core.seed import SeedError, parse_seed

from bernstein.core.teams.manifest import (
    TeamManifestDigestMismatchError,
    TeamManifestNotFoundError,
    load_team_manifest,
)

MANIFEST = """\
name = "crew"
version = "1.0.0"

[coordination]
parallelism = 2
review_chain = true

[[roles]]
role = "backend"
response_profile = "terse"

[roles.model_policy]
model = "sonnet"
effort = "high"

[[roles]]
role = "qa"

[roles.model_policy]
model = "sonnet"
"""


@pytest.fixture()
def workdir(tmp_path: Path) -> Path:
    teams_dir = tmp_path / "templates" / "teams"
    teams_dir.mkdir(parents=True)
    (teams_dir / "crew.toml").write_text(MANIFEST, encoding="utf-8")
    return tmp_path


def _write_seed(workdir: Path, body: str) -> Path:
    seed = workdir / "bernstein.yaml"
    seed.write_text(body, encoding="utf-8")
    return seed


class TestTeamManifestExpansion:
    def test_manifest_expands_to_team_and_policy(self, workdir: Path) -> None:
        cfg = parse_seed(_write_seed(workdir, 'goal: "T"\nteam_manifest: crew\n'))
        assert cfg.team == ["backend", "qa"]
        assert cfg.role_model_policy == {
            "backend": {"model": "sonnet", "effort": "high", "response_style": "terse"},
            "qa": {"model": "sonnet"},
        }
        assert cfg.team_manifest == "crew"
        assert cfg.team_manifest_digest is not None
        assert len(cfg.team_manifest_digest) == 64

    def test_expansion_is_deterministic_across_parses(self, workdir: Path) -> None:
        """AC1: the same manifest expands identically on every parse."""
        seed = _write_seed(workdir, 'goal: "T"\nteam_manifest: crew\n')
        first = parse_seed(seed)
        second = parse_seed(seed)
        assert first.team == second.team
        assert first.role_model_policy == second.role_model_policy
        assert first.team_manifest_digest == second.team_manifest_digest

    def test_expansion_matches_hand_written_seed(self, workdir: Path) -> None:
        """AC1: manifest expansion produces the existing structures exactly."""
        manifest_cfg = parse_seed(_write_seed(workdir, 'goal: "T"\nteam_manifest: crew\n'))
        inline = (
            'goal: "T"\n'
            "team: [backend, qa]\n"
            "role_model_policy:\n"
            "  backend:\n"
            "    model: sonnet\n"
            "    effort: high\n"
            "    response_style: terse\n"
            "  qa:\n"
            "    model: sonnet\n"
        )
        inline_dir = workdir / "inline"
        inline_dir.mkdir()
        inline_cfg = parse_seed(_write_seed(inline_dir, inline))
        assert manifest_cfg.team == inline_cfg.team
        assert manifest_cfg.role_model_policy == inline_cfg.role_model_policy

    def test_seed_role_policy_overrides_manifest_per_key(self, workdir: Path) -> None:
        body = 'goal: "T"\nteam_manifest: crew\nrole_model_policy:\n  backend:\n    model: opus\n'
        cfg = parse_seed(_write_seed(workdir, body))
        assert cfg.role_model_policy["backend"]["model"] == "opus"
        # Manifest-supplied keys not overridden by the seed survive.
        assert cfg.role_model_policy["backend"]["effort"] == "high"
        assert cfg.role_model_policy["backend"]["response_style"] == "terse"

    def test_seed_can_add_roles_not_in_manifest(self, workdir: Path) -> None:
        body = 'goal: "T"\nteam_manifest: crew\nrole_model_policy:\n  default:\n    model: opus\n'
        cfg = parse_seed(_write_seed(workdir, body))
        assert cfg.role_model_policy["default"] == {"model": "opus"}

    def test_matching_digest_pin_parses(self, workdir: Path) -> None:
        digest = load_team_manifest(workdir / "templates" / "teams" / "crew.toml").digest()
        cfg = parse_seed(_write_seed(workdir, f'goal: "T"\nteam_manifest: crew@{digest}\n'))
        assert cfg.team_manifest == "crew"
        assert cfg.team_manifest_digest == digest

    def test_inline_team_defaults_do_not_conflict(self, workdir: Path) -> None:
        cfg = parse_seed(_write_seed(workdir, 'goal: "T"\nteam: auto\nteam_manifest: crew\n'))
        assert cfg.team == ["backend", "qa"]

    def test_manifest_without_policy_keys_leaves_policy_none(self, tmp_path: Path) -> None:
        teams_dir = tmp_path / "templates" / "teams"
        teams_dir.mkdir(parents=True)
        (teams_dir / "bare.toml").write_text(
            'name = "bare"\nversion = "1"\n\n[[roles]]\nrole = "qa"\n', encoding="utf-8"
        )
        cfg = parse_seed(_write_seed(tmp_path, 'goal: "T"\nteam_manifest: bare\n'))
        assert cfg.team == ["qa"]
        assert cfg.role_model_policy is None

    def test_builtin_python_manifest_resolves(self, tmp_path: Path) -> None:
        cfg = parse_seed(_write_seed(tmp_path, 'goal: "T"\nteam_manifest: python\n'))
        assert cfg.team == ["backend", "qa", "security"]
        assert cfg.role_model_policy["security"]["model"] == "opus"


class TestTeamManifestRefusals:
    def test_mismatched_digest_refuses_with_typed_error(self, workdir: Path) -> None:
        """AC4: a wrong pin is a typed refusal, not a warning."""
        wrong = "0" * 64
        seed = _write_seed(workdir, f'goal: "T"\nteam_manifest: crew@{wrong}\n')
        with pytest.raises(TeamManifestDigestMismatchError, match="digest mismatch"):
            parse_seed(seed)

    def test_mismatched_digest_is_also_a_seed_error(self, workdir: Path) -> None:
        """Existing SeedError handlers (the run refusal path) still catch it."""
        wrong = "0" * 64
        seed = _write_seed(workdir, f'goal: "T"\nteam_manifest: crew@{wrong}\n')
        with pytest.raises(SeedError):
            parse_seed(seed)

    def test_unknown_manifest_name_refuses_with_typed_error(self, workdir: Path) -> None:
        seed = _write_seed(workdir, 'goal: "T"\nteam_manifest: no-such-team\n')
        with pytest.raises(TeamManifestNotFoundError):
            parse_seed(seed)

    def test_inline_team_list_conflicts_with_manifest(self, workdir: Path) -> None:
        seed = _write_seed(workdir, 'goal: "T"\nteam: [backend]\nteam_manifest: crew\n')
        with pytest.raises(SeedError, match="mutually exclusive"):
            parse_seed(seed)

    @pytest.mark.parametrize("ref", ["", "a/b", "crew@nothex"])
    def test_malformed_reference_refuses(self, workdir: Path, ref: str) -> None:
        seed = _write_seed(workdir, f'goal: "T"\nteam_manifest: "{ref}"\n')
        with pytest.raises(SeedError):
            parse_seed(seed)

    def test_non_string_reference_refuses(self, workdir: Path) -> None:
        seed = _write_seed(workdir, 'goal: "T"\nteam_manifest: [crew]\n')
        with pytest.raises(SeedError, match="team_manifest"):
            parse_seed(seed)

    def test_manifest_policy_flows_through_standard_validation(self, tmp_path: Path) -> None:
        """Expanded policies hit the same validator as hand-written ones."""
        teams_dir = tmp_path / "templates" / "teams"
        teams_dir.mkdir(parents=True)
        (teams_dir / "bad.toml").write_text(
            'name = "bad"\nversion = "1"\n\n[[roles]]\nrole = "qa"\nresponse_profile = "shouty"\n',
            encoding="utf-8",
        )
        seed = _write_seed(tmp_path, 'goal: "T"\nteam_manifest: bad\n')
        with pytest.raises(SeedError, match="response_style"):
            parse_seed(seed)
