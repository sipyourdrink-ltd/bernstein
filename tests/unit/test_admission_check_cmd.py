"""``bernstein admission check`` decision-table tests (#4907).

The command must answer, without spawning anything, which of the roles a
config declares the admission policy would admit - and name the rule
that decided each one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.admission_cmd import admission_group

_POLICY = "admission:\n  rules:\n    - id: approved-adapters\n      effect: allow\n      adapters: [claude]\n"

_ROLES = (
    "role_model_policy:\n"
    "  backend:\n"
    "    cli: claude\n"
    "    model: claude-sonnet-4\n"
    "  researcher:\n"
    "    cli: codex\n"
    "    model: gpt-5-codex\n"
)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A repository root whose bernstein.yaml declares roles and a policy."""
    (tmp_path / "bernstein.yaml").write_text(
        "goal: ship the thing\ncli: claude\n" + _ROLES + _POLICY,
        encoding="utf-8",
    )
    return tmp_path


def _run(repo: Path, *args: str) -> tuple[int, str]:
    """Invoke ``admission check`` in *repo* and return (exit code, output)."""
    result = CliRunner().invoke(admission_group, ["check", "--workdir", str(repo), *args])
    return result.exit_code, result.output


class TestDecisionTable:
    """One row per configured role, each naming the deciding rule."""

    def test_check_prints_a_decision_per_role_without_spawning(self, repo: Path) -> None:
        code, output = _run(repo)

        assert "backend" in output
        assert "researcher" in output
        assert "approved-adapters" in output
        # The researcher role pins codex, which no allow rule admits.
        assert code == 1, output

    def test_refused_role_makes_the_command_exit_non_zero(self, repo: Path) -> None:
        code, _ = _run(repo, "--role", "researcher")

        assert code == 1

    def test_admitted_role_makes_the_command_exit_zero(self, repo: Path) -> None:
        code, output = _run(repo, "--role", "backend")

        assert code == 0, output
        assert "approved-adapters" in output

    def test_axis_override_evaluates_the_hypothetical_subject(self, repo: Path) -> None:
        code, output = _run(repo, "--role", "researcher", "--adapter", "claude", "--json")

        assert code == 0, output
        rows = json.loads(output)["rows"]
        assert rows[0]["adapter"] == "claude"
        assert rows[0]["rule_id"] == "approved-adapters"


class TestNoPolicy:
    """A config with no policy reports that plainly and succeeds."""

    def test_config_without_admission_block_reports_no_policy(self, tmp_path: Path) -> None:
        (tmp_path / "bernstein.yaml").write_text("goal: ship the thing\n", encoding="utf-8")

        code, output = _run(tmp_path)

        assert code == 0
        assert "No admission policy declared" in output


class TestBadConfig:
    """A malformed policy is reported, never silently treated as absent."""

    def test_malformed_policy_is_reported(self, tmp_path: Path) -> None:
        (tmp_path / "bernstein.yaml").write_text(
            "goal: ship the thing\nadmission:\n  rules:\n    - id: r\n      effect: permit\n",
            encoding="utf-8",
        )

        code, output = _run(tmp_path)

        assert code != 0
        assert "effect" in output
