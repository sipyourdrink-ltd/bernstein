"""Tests for bernstein.core.seed."""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.models import Complexity, Scope, TaskStatus
from bernstein.core.seed import (
    NotifyConfig,
    SeedConfig,
    SeedError,
    _build_manager_description,
    parse_seed,
    seed_to_initial_task,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MINIMAL_YAML = 'goal: "Build a REST API"\n'

FULL_YAML = """\
goal: "Build a REST API"
budget: "$20"
team: [backend, qa, devops]
cli: codex
max_agents: 4
model: gpt-4.1
"""

AUTO_TEAM_YAML = """\
goal: "Deploy the thing"
team: auto
"""

BARE_BUDGET_YAML = """\
goal: "Test"
budget: 35
"""


@pytest.fixture()
def seed_file(tmp_path: Path) -> Path:
    """Return path to a seed file (content written per-test)."""
    return tmp_path / "bernstein.yaml"


# ---------------------------------------------------------------------------
# parse_seed — valid inputs
# ---------------------------------------------------------------------------


class TestParseSeedValid:
    """Tests for valid seed file parsing."""

    def test_minimal_yaml_defaults(self, seed_file: Path) -> None:
        seed_file.write_text(MINIMAL_YAML)
        cfg = parse_seed(seed_file)
        assert cfg.goal == "Build a REST API"
        assert cfg.budget_usd is None
        assert cfg.team == "auto"
        assert cfg.cli == "auto"
        assert cfg.max_agents == 6
        assert cfg.model is None

    def test_full_yaml_all_fields(self, seed_file: Path) -> None:
        seed_file.write_text(FULL_YAML)
        cfg = parse_seed(seed_file)
        assert cfg.goal == "Build a REST API"
        assert cfg.budget_usd == 20.0
        assert cfg.team == ["backend", "qa", "devops"]
        assert cfg.cli == "codex"
        assert cfg.max_agents == 4
        assert cfg.model == "gpt-4.1"

    def test_auto_team_explicit(self, seed_file: Path) -> None:
        seed_file.write_text(AUTO_TEAM_YAML)
        cfg = parse_seed(seed_file)
        assert cfg.team == "auto"

    def test_bare_numeric_budget(self, seed_file: Path) -> None:
        seed_file.write_text(BARE_BUDGET_YAML)
        cfg = parse_seed(seed_file)
        assert cfg.budget_usd == 35.0

    def test_budget_as_float(self, seed_file: Path) -> None:
        seed_file.write_text('goal: "T"\nbudget: 9.99\n')
        cfg = parse_seed(seed_file)
        assert cfg.budget_usd == pytest.approx(9.99)

    def test_budget_as_dollar_string_with_decimals(self, seed_file: Path) -> None:
        seed_file.write_text('goal: "T"\nbudget: "$12.50"\n')
        cfg = parse_seed(seed_file)
        assert cfg.budget_usd == pytest.approx(12.50)

    def test_empty_team_list_becomes_auto(self, seed_file: Path) -> None:
        seed_file.write_text('goal: "T"\nteam: []\n')
        cfg = parse_seed(seed_file)
        assert cfg.team == "auto"

    def test_gemini_cli(self, seed_file: Path) -> None:
        seed_file.write_text('goal: "T"\ncli: gemini\n')
        cfg = parse_seed(seed_file)
        assert cfg.cli == "gemini"

    def test_role_model_policy_parsed(self, seed_file: Path) -> None:
        seed_file.write_text(
            'goal: "T"\n'
            "role_model_policy:\n"
            "  backend:\n"
            "    provider: codex\n"
            "    model: gpt-5.4-mini\n"
            "    effort: high\n"
        )
        cfg = parse_seed(seed_file)
        assert cfg.role_model_policy == {
            "backend": {"provider": "codex", "model": "gpt-5.4-mini", "effort": "high"}
        }


# ---------------------------------------------------------------------------
# parse_seed — invalid inputs
# ---------------------------------------------------------------------------


class TestParseSeedInvalid:
    """Tests for seed file validation errors."""

    def test_missing_file_raises_seed_error(self, seed_file: Path) -> None:
        with pytest.raises(SeedError, match="Seed file not found"):
            parse_seed(seed_file)

    def test_missing_goal_raises_seed_error(self, seed_file: Path) -> None:
        seed_file.write_text("budget: 10\n")
        with pytest.raises(SeedError, match="goal"):
            parse_seed(seed_file)

    def test_empty_goal_raises_seed_error(self, seed_file: Path) -> None:
        seed_file.write_text('goal: ""\n')
        with pytest.raises(SeedError, match="goal"):
            parse_seed(seed_file)

    def test_invalid_yaml_raises_seed_error(self, seed_file: Path) -> None:
        seed_file.write_text("goal: [\ninvalid yaml {{{\n")
        with pytest.raises(SeedError, match="Invalid YAML"):
            parse_seed(seed_file)

    def test_invalid_cli_raises_seed_error(self, seed_file: Path) -> None:
        seed_file.write_text('goal: "T"\ncli: chatgpt\n')
        with pytest.raises(SeedError, match="cli must be one of"):
            parse_seed(seed_file)

    def test_invalid_budget_format_raises_seed_error(self, seed_file: Path) -> None:
        seed_file.write_text('goal: "T"\nbudget: "free"\n')
        with pytest.raises(SeedError, match="Invalid budget format"):
            parse_seed(seed_file)

    def test_max_agents_zero_raises_seed_error(self, seed_file: Path) -> None:
        seed_file.write_text('goal: "T"\nmax_agents: 0\n')
        with pytest.raises(SeedError, match="max_agents must be a positive integer"):
            parse_seed(seed_file)

    def test_non_mapping_yaml_raises_seed_error(self, seed_file: Path) -> None:
        seed_file.write_text("- item1\n- item2\n")
        with pytest.raises(SeedError, match="YAML mapping"):
            parse_seed(seed_file)

    def test_team_with_non_string_items_raises_seed_error(self, seed_file: Path) -> None:
        seed_file.write_text('goal: "T"\nteam: [1, 2, 3]\n')
        with pytest.raises(SeedError, match="team list must contain only strings"):
            parse_seed(seed_file)

    def test_invalid_role_model_policy_shape_raises(self, seed_file: Path) -> None:
        seed_file.write_text('goal: "T"\nrole_model_policy: "bad"\n')
        with pytest.raises(SeedError, match="role_model_policy must be a mapping"):
            parse_seed(seed_file)


# ---------------------------------------------------------------------------
# parse_seed — worktree_setup
# ---------------------------------------------------------------------------


class TestParseSeedWorktreeSetup:
    """Tests for worktree_setup section parsing."""

    def test_no_worktree_setup_defaults_to_none(self, seed_file: Path) -> None:
        seed_file.write_text(MINIMAL_YAML)
        cfg = parse_seed(seed_file)
        assert cfg.worktree_setup is None

    def test_symlink_dirs_parsed(self, seed_file: Path) -> None:
        seed_file.write_text('goal: "T"\nworktree_setup:\n  symlink_dirs: [node_modules, .venv]\n')
        cfg = parse_seed(seed_file)
        assert cfg.worktree_setup is not None
        assert cfg.worktree_setup.symlink_dirs == ("node_modules", ".venv")

    def test_copy_files_parsed(self, seed_file: Path) -> None:
        seed_file.write_text('goal: "T"\nworktree_setup:\n  copy_files: [.env, .env.local]\n')
        cfg = parse_seed(seed_file)
        assert cfg.worktree_setup is not None
        assert cfg.worktree_setup.copy_files == (".env", ".env.local")

    def test_setup_command_parsed(self, seed_file: Path) -> None:
        seed_file.write_text('goal: "T"\nworktree_setup:\n  setup_command: "uv sync"\n')
        cfg = parse_seed(seed_file)
        assert cfg.worktree_setup is not None
        assert cfg.worktree_setup.setup_command == "uv sync"

    def test_all_fields_together(self, seed_file: Path) -> None:
        seed_file.write_text(
            "goal: T\n"
            "worktree_setup:\n"
            "  symlink_dirs: [node_modules]\n"
            "  copy_files: [.env]\n"
            "  setup_command: npm install\n"
        )
        cfg = parse_seed(seed_file)
        assert cfg.worktree_setup is not None
        assert cfg.worktree_setup.symlink_dirs == ("node_modules",)
        assert cfg.worktree_setup.copy_files == (".env",)
        assert cfg.worktree_setup.setup_command == "npm install"

    def test_empty_worktree_setup_block(self, seed_file: Path) -> None:
        """An empty worktree_setup block creates a default config with no dirs/files."""
        seed_file.write_text("goal: T\nworktree_setup: {}\n")
        cfg = parse_seed(seed_file)
        assert cfg.worktree_setup is not None
        assert cfg.worktree_setup.symlink_dirs == ()
        assert cfg.worktree_setup.copy_files == ()
        assert cfg.worktree_setup.setup_command is None

    def test_non_mapping_worktree_setup_raises(self, seed_file: Path) -> None:
        seed_file.write_text('goal: "T"\nworktree_setup: "invalid"\n')
        with pytest.raises(SeedError, match="worktree_setup must be a mapping"):
            parse_seed(seed_file)

    def test_non_list_symlink_dirs_raises(self, seed_file: Path) -> None:
        seed_file.write_text('goal: "T"\nworktree_setup:\n  symlink_dirs: "node_modules"\n')
        with pytest.raises(SeedError, match="worktree_setup.symlink_dirs"):
            parse_seed(seed_file)

    def test_non_string_setup_command_raises(self, seed_file: Path) -> None:
        seed_file.write_text('goal: "T"\nworktree_setup:\n  setup_command: 42\n')
        with pytest.raises(SeedError, match="worktree_setup.setup_command must be a string"):
            parse_seed(seed_file)


# ---------------------------------------------------------------------------
# seed_to_initial_task
# ---------------------------------------------------------------------------


class TestSeedToInitialTask:
    """Tests for initial task creation from seed config."""

    def test_creates_manager_task(self) -> None:
        cfg = SeedConfig(goal="Build a REST API")
        task = seed_to_initial_task(cfg)
        assert task.role == "manager"
        assert "Build a REST API" in task.description
        assert task.priority == 10
        assert task.id == "task-000"
        assert task.title == "Initial goal"

    def test_task_status_is_open(self) -> None:
        cfg = SeedConfig(goal="Deploy infra")
        task = seed_to_initial_task(cfg)
        assert task.status == TaskStatus.OPEN

    def test_task_scope_and_complexity(self) -> None:
        cfg = SeedConfig(goal="Refactor everything")
        task = seed_to_initial_task(cfg)
        assert task.scope == Scope.LARGE
        assert task.complexity == Complexity.HIGH

    def test_different_goals_produce_different_descriptions(self) -> None:
        t1 = seed_to_initial_task(SeedConfig(goal="Goal A"))
        t2 = seed_to_initial_task(SeedConfig(goal="Goal B"))
        assert t1.description != t2.description


# ---------------------------------------------------------------------------
# SeedConfig dataclass
# ---------------------------------------------------------------------------


class TestSeedConfig:
    """Tests for SeedConfig defaults and immutability."""

    def test_defaults(self) -> None:
        cfg = SeedConfig(goal="Test")
        assert cfg.budget_usd is None
        assert cfg.team == "auto"
        assert cfg.cli == "auto"
        assert cfg.max_agents == 6
        assert cfg.model is None

    def test_frozen(self) -> None:
        cfg = SeedConfig(goal="Test")
        with pytest.raises(AttributeError):
            cfg.goal = "Changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# NotifyConfig parsing
# ---------------------------------------------------------------------------

NOTIFY_YAML = """\
goal: "Build a REST API"
notify:
  webhook: https://hooks.slack.com/services/T.../B.../xxx
  on_complete: true
  on_failure: false
"""

NOTIFY_WEBHOOK_ONLY_YAML = """\
goal: "Build a REST API"
notify:
  webhook: https://hooks.example.com/notify
"""


class TestNotifyConfig:
    """Tests for NotifyConfig parsing."""

    def test_notify_parsed_with_all_fields(self, seed_file: Path) -> None:
        seed_file.write_text(NOTIFY_YAML)
        cfg = parse_seed(seed_file)
        assert cfg.notify is not None
        assert cfg.notify.webhook_url == "https://hooks.slack.com/services/T.../B.../xxx"
        assert cfg.notify.on_complete is True
        assert cfg.notify.on_failure is False

    def test_notify_webhook_only_defaults(self, seed_file: Path) -> None:
        seed_file.write_text(NOTIFY_WEBHOOK_ONLY_YAML)
        cfg = parse_seed(seed_file)
        assert cfg.notify is not None
        assert cfg.notify.webhook_url == "https://hooks.example.com/notify"
        assert cfg.notify.on_complete is True
        assert cfg.notify.on_failure is True

    def test_notify_absent_is_none(self, seed_file: Path) -> None:
        seed_file.write_text(MINIMAL_YAML)
        cfg = parse_seed(seed_file)
        assert cfg.notify is None

    def test_notify_not_a_mapping_raises(self, seed_file: Path) -> None:
        seed_file.write_text('goal: "T"\nnotify: "https://example.com"\n')
        with pytest.raises(SeedError, match="notify must be a mapping"):
            parse_seed(seed_file)

    def test_notify_webhook_not_string_raises(self, seed_file: Path) -> None:
        seed_file.write_text('goal: "T"\nnotify:\n  webhook: 123\n')
        with pytest.raises(SeedError, match="notify.webhook must be a string"):
            parse_seed(seed_file)

    def test_notify_config_defaults(self) -> None:
        nc = NotifyConfig(webhook_url="https://example.com")
        assert nc.on_complete is True
        assert nc.on_failure is True

    def test_notify_config_frozen(self) -> None:
        nc = NotifyConfig(webhook_url="https://example.com")
        with pytest.raises(AttributeError):
            nc.webhook_url = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# _build_manager_description
# ---------------------------------------------------------------------------


class TestBuildManagerDescription:
    """Tests for _build_manager_description()."""

    def test_goal_only(self) -> None:
        cfg = SeedConfig(goal="Build a REST API")
        result = _build_manager_description(cfg, workdir=None)
        assert "## Goal" in result
        assert "Build a REST API" in result
        assert "## Team" not in result
        assert "## Budget" not in result
        assert "## Constraints" not in result
        assert "## Context files" not in result

    def test_with_team_list(self) -> None:
        cfg = SeedConfig(goal="Deploy infra", team=["backend", "qa", "devops"])
        result = _build_manager_description(cfg, workdir=None)
        assert "## Team" in result
        assert "backend" in result
        assert "qa" in result
        assert "devops" in result

    def test_auto_team_omitted(self) -> None:
        cfg = SeedConfig(goal="Deploy infra", team="auto")
        result = _build_manager_description(cfg, workdir=None)
        assert "## Team" not in result

    def test_with_budget(self) -> None:
        cfg = SeedConfig(goal="Build X", budget_usd=42.50)
        result = _build_manager_description(cfg, workdir=None)
        assert "## Budget" in result
        assert "42.50" in result

    def test_with_constraints(self) -> None:
        cfg = SeedConfig(goal="Build X", constraints=("Python only", "No external APIs"))
        result = _build_manager_description(cfg, workdir=None)
        assert "## Constraints" in result
        assert "- Python only" in result
        assert "- No external APIs" in result

    def test_with_context_files_real_files(self, tmp_path: Path) -> None:
        (tmp_path / "spec.md").write_text("# My spec\nHello world")
        cfg = SeedConfig(goal="Build X", context_files=("spec.md",))
        result = _build_manager_description(cfg, workdir=tmp_path)
        assert "## Context files" in result
        assert "spec.md" in result
        assert "# My spec" in result
        assert "Hello world" in result
        assert "```" in result

    def test_with_context_files_missing_file(self, tmp_path: Path) -> None:
        cfg = SeedConfig(goal="Build X", context_files=("missing.md",))
        result = _build_manager_description(cfg, workdir=tmp_path)
        assert "## Context files" in result
        assert "missing.md" in result
        assert "(file not found)" in result

    def test_with_context_files_unreadable_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        target = tmp_path / "secret.txt"
        target.write_text("contents")
        original_read_text = Path.read_text

        def mock_read_text(self: Path, *args: object, **kwargs: object) -> str:
            if self == target:
                raise OSError("Permission denied")
            return original_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", mock_read_text)
        cfg = SeedConfig(goal="Build X", context_files=("secret.txt",))
        result = _build_manager_description(cfg, workdir=tmp_path)
        assert "## Context files" in result
        assert "secret.txt" in result
        assert "(could not read file)" in result

    def test_context_files_ignored_when_no_workdir(self) -> None:
        cfg = SeedConfig(goal="Build X", context_files=("spec.md",))
        result = _build_manager_description(cfg, workdir=None)
        assert "## Context files" not in result

    def test_all_fields_combined(self, tmp_path: Path) -> None:
        (tmp_path / "notes.txt").write_text("Important notes here")
        cfg = SeedConfig(
            goal="Build everything",
            team=["backend", "frontend"],
            budget_usd=100.00,
            constraints=("No TypeScript",),
            context_files=("notes.txt",),
        )
        result = _build_manager_description(cfg, workdir=tmp_path)
        assert "## Goal" in result
        assert "Build everything" in result
        assert "## Team" in result
        assert "backend" in result
        assert "## Budget" in result
        assert "100.00" in result
        assert "## Constraints" in result
        assert "No TypeScript" in result
        assert "## Context files" in result
        assert "Important notes here" in result
