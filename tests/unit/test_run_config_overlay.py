"""Regression tests for #4485: run configuration is never part of a change.

A run used to pin its own overrides into the tracked ``bernstein.yaml`` in
the work tree.  Any agent that committed with a staged tree carried the file,
and the resulting pull request proposed rewriting the repository's committed
configuration for everyone.  One publishing path restored the file before
publishing; every other path that pushes commits did not, so the leak
returned as soon as a second path existed.

These tests hold the invariant itself rather than the compensating step: run
overrides resolve from a layer git does not track, the one file that must
live in the work tree cannot be staged, and a change that contains a
configuration path is refused with the file named.

The work-tree and commit properties run against real temporary git
repositories - ``git status``, ``git add -A`` and ``git commit -a`` are the
things under test, and a mock of them would assert nothing.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import bernstein.core.git.git_pr as git_pr
from bernstein.core.config.run_overlay import (
    ENV_CONFIG_OVERLAY,
    ENV_CONFIG_OVERRIDE,
    RUN_CONFIG_PATHS,
    RunOverlayError,
    effective_mtime,
    resolve_overlay_path,
    write_overlay,
)
from bernstein.core.config.seed import SeedError, parse_seed
from bernstein.core.git.git_basic import stage_all_except
from bernstein.core.git.local_exclude import RUN_EXCLUDE_ENTRIES, register_run_excludes
from bernstein.core.quality.gate_pipeline import build_default_pipeline
from bernstein.core.quality.quality_gates import QualityGatesConfig
from bernstein.core.quality.run_config_gate import UNREADABLE, check_commit, check_paths, check_staged

COMMITTED_SEED = """goal: "ship the feature"
max_agents: 3
budget: "$5"
internal_llm_provider: none
role_model_policy:
  backend:
    model: sonnet
  qa:
    model: haiku
"""


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout


def _staged(repo: Path) -> list[str]:
    out = _git(repo, "diff", "--cached", "--name-only")
    return [line.strip() for line in out.splitlines() if line.strip()]


@pytest.fixture(autouse=True)
def _isolated_overlay_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neither overlay variable leaks in from the developer's environment."""
    for name in (ENV_CONFIG_OVERLAY, ENV_CONFIG_OVERRIDE, "GIT_DIR", "GIT_WORK_TREE"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real repository with a committed configuration file and one source file."""
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test User")
    (root / "bernstein.yaml").write_text(COMMITTED_SEED, encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "feature.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "initial commit")
    return root


# ---------------------------------------------------------------------------
# The work tree stays clean, and no commit can carry configuration
# ---------------------------------------------------------------------------


def test_overlay_never_dirties_the_work_tree(repo: Path) -> None:
    """An active overlay leaves ``git status`` with nothing to report."""
    config_path = repo / "bernstein.yaml"
    before = config_path.read_text(encoding="utf-8")

    write_overlay({"max_agents": 9, "internal_llm_provider": "anthropic"}, config_path=config_path)

    assert _git(repo, "status", "--porcelain") == "", "an overlay write must not show up as a work-tree change"
    assert config_path.read_text(encoding="utf-8") == before, (
        "the committed file must be byte-identical after a run override"
    )
    assert parse_seed(config_path).max_agents == 9, "the override must nevertheless be in effect"


def test_commit_all_after_an_overlay_write_contains_no_configuration_file(repo: Path) -> None:
    """``git commit -a`` over an active overlay produces a config-free commit."""
    config_path = repo / "bernstein.yaml"
    write_overlay({"max_agents": 12}, config_path=config_path)

    (repo / "src" / "feature.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git(repo, "commit", "-a", "-m", "feat: bump the value")

    changed = [
        line.strip()
        for line in _git(repo, "show", "--name-only", "--pretty=format:", "HEAD").splitlines()
        if line.strip()
    ]
    assert changed == ["src/feature.py"]
    assert check_commit(repo).ok


def test_overlay_lives_inside_the_git_directory_not_the_work_tree(repo: Path) -> None:
    """The default overlay location is one git can never track."""
    config_path = repo / "bernstein.yaml"
    overlay_path = write_overlay({"max_agents": 4}, config_path=config_path)

    assert overlay_path.is_file()
    assert overlay_path.is_relative_to(repo / ".git"), f"overlay must live inside .git/, got {overlay_path}"
    tracked_and_untracked = _git(repo, "status", "--porcelain", "--untracked-files=all")
    assert "run-overlay" not in tracked_and_untracked


def test_overlay_path_inside_the_work_tree_is_refused(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An overlay pointed at the work tree is the original defect, renamed."""
    monkeypatch.setenv(ENV_CONFIG_OVERLAY, str(repo / "run-overlay.yaml"))

    with pytest.raises(RunOverlayError, match="inside the work tree"):
        write_overlay({"max_agents": 4}, config_path=repo / "bernstein.yaml")

    assert _git(repo, "status", "--porcelain") == ""


def test_overlay_path_reaching_the_work_tree_through_a_symlink_is_refused(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A different spelling of a work-tree path is still a work-tree path."""
    alias = tmp_path / "alias"
    alias.symlink_to(repo, target_is_directory=True)
    monkeypatch.setenv(ENV_CONFIG_OVERLAY, str(alias / "run-overlay.yaml"))

    with pytest.raises(RunOverlayError, match="inside the work tree"):
        write_overlay({"max_agents": 4}, config_path=repo / "bernstein.yaml")

    assert _git(repo, "status", "--porcelain") == ""


def test_sandbox_supplied_overlay_path_is_honoured(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A sandbox can place the overlay outside the repository entirely."""
    sandbox_overlay = tmp_path / "sandbox" / "overlay.yaml"
    monkeypatch.setenv(ENV_CONFIG_OVERLAY, str(sandbox_overlay))
    config_path = repo / "bernstein.yaml"

    assert resolve_overlay_path(config_path) == sandbox_overlay
    write_overlay({"max_agents": 7}, config_path=config_path)

    assert sandbox_overlay.is_file()
    assert parse_seed(config_path).max_agents == 7
    assert _git(repo, "status", "--porcelain") == ""


# ---------------------------------------------------------------------------
# Effective configuration and precedence
# ---------------------------------------------------------------------------


def test_effective_config_equals_committed_merged_with_overlay(repo: Path) -> None:
    """Keys the overlay does not mention keep their committed values."""
    config_path = repo / "bernstein.yaml"
    committed = parse_seed(config_path)

    write_overlay({"max_agents": 11}, config_path=config_path)
    effective = parse_seed(config_path)

    assert effective.max_agents == 11, "the overlay key wins"
    assert effective.goal == committed.goal
    assert effective.budget_usd == committed.budget_usd
    assert effective.role_model_policy == committed.role_model_policy


def test_precedence_is_inline_override_then_overlay_then_committed_file(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All three layers set the same key; the documented order decides."""
    config_path = repo / "bernstein.yaml"
    assert parse_seed(config_path).max_agents == 3, "committed file alone"

    write_overlay({"max_agents": 5}, config_path=config_path)
    assert parse_seed(config_path).max_agents == 5, "overlay beats the committed file"

    monkeypatch.setenv(ENV_CONFIG_OVERRIDE, "{max_agents: 8}")
    assert parse_seed(config_path).max_agents == 8, "an explicit env override beats the overlay"


def test_nested_sections_merge_key_by_key_instead_of_being_replaced(repo: Path) -> None:
    """Overriding one role must not rewrite every entry of ``role_model_policy``."""
    config_path = repo / "bernstein.yaml"
    write_overlay({"role_model_policy": {"backend": {"model": "opus"}}}, config_path=config_path)

    policy = parse_seed(config_path).role_model_policy

    assert policy is not None
    assert policy["backend"]["model"] == "opus"
    assert policy["qa"]["model"] == "haiku", "roles the overlay did not mention keep their committed values"


def test_repeated_overlay_writes_accumulate_rather_than_replace(repo: Path) -> None:
    """A second override does not silently drop the first."""
    config_path = repo / "bernstein.yaml"
    write_overlay({"max_agents": 6}, config_path=config_path)
    write_overlay({"internal_llm_provider": "anthropic"}, config_path=config_path)

    effective = parse_seed(config_path)

    assert effective.max_agents == 6
    assert effective.internal_llm_provider == "anthropic"


def test_overlay_write_advances_the_hot_reload_mtime(repo: Path) -> None:
    """Watching only the tracked file would never notice a run's own change."""
    config_path = repo / "bernstein.yaml"
    before = effective_mtime(config_path)

    write_overlay({"max_agents": 6}, config_path=config_path)

    overlay_path = resolve_overlay_path(config_path)
    assert overlay_path is not None
    assert effective_mtime(config_path) >= before
    assert effective_mtime(config_path) == max(config_path.stat().st_mtime, overlay_path.stat().st_mtime)


# ---------------------------------------------------------------------------
# Setups that never adopt the overlay keep working
# ---------------------------------------------------------------------------


def test_editing_the_committed_file_directly_still_takes_effect(repo: Path) -> None:
    """The overlay is additive: with none present, nothing about loading changes."""
    config_path = repo / "bernstein.yaml"
    config_path.write_text(COMMITTED_SEED.replace("max_agents: 3", "max_agents: 21"), encoding="utf-8")

    effective = parse_seed(config_path)

    assert resolve_overlay_path(config_path) is not None, "a location exists"
    assert not resolve_overlay_path(config_path).exists(), "but no overlay file was written"
    assert effective.max_agents == 21
    assert effective.role_model_policy == {"backend": {"model": "sonnet"}, "qa": {"model": "haiku"}}


def test_config_outside_a_git_repository_loads_without_an_overlay(tmp_path: Path) -> None:
    """No repository means no overlay location, and the committed file stands alone."""
    config_path = tmp_path / "bernstein.yaml"
    config_path.write_text(COMMITTED_SEED, encoding="utf-8")

    assert resolve_overlay_path(config_path) is None
    assert parse_seed(config_path).max_agents == 3


def test_malformed_overlay_fails_loudly_instead_of_falling_back(repo: Path) -> None:
    """A run must not proceed on a configuration nobody asked for."""
    config_path = repo / "bernstein.yaml"
    overlay_path = resolve_overlay_path(config_path)
    assert overlay_path is not None
    overlay_path.parent.mkdir(parents=True, exist_ok=True)
    overlay_path.write_text("max_agents: [unclosed\n", encoding="utf-8")

    with pytest.raises(SeedError, match="overlay"):
        parse_seed(config_path)


def test_overlay_that_is_not_a_mapping_fails_loudly(repo: Path) -> None:
    """A YAML list where a mapping was expected is an error, not an empty overlay."""
    config_path = repo / "bernstein.yaml"
    overlay_path = resolve_overlay_path(config_path)
    assert overlay_path is not None
    overlay_path.parent.mkdir(parents=True, exist_ok=True)
    overlay_path.write_text("- max_agents\n- 4\n", encoding="utf-8")

    with pytest.raises(SeedError, match="must be a mapping"):
        parse_seed(config_path)


# ---------------------------------------------------------------------------
# The commit gate
# ---------------------------------------------------------------------------


def test_gate_rejects_a_staged_run_configuration_path_and_names_the_file(repo: Path) -> None:
    """Staging the configuration file stops the commit, with the path in the message."""
    (repo / "bernstein.yaml").write_text(COMMITTED_SEED.replace("max_agents: 3", "max_agents: 99"), encoding="utf-8")
    _git(repo, "add", "bernstein.yaml")

    verdict = check_staged(repo)

    assert verdict.ok is False
    assert verdict.offending_paths == ("bernstein.yaml",)
    assert "bernstein.yaml" in verdict.details


def test_gate_rejects_a_commit_whose_diff_touches_run_configuration(repo: Path) -> None:
    """A commit that already exists is caught with the same message."""
    (repo / "bernstein.yaml").write_text(COMMITTED_SEED.replace("max_agents: 3", "max_agents: 99"), encoding="utf-8")
    _git(repo, "commit", "-a", "-m", "chore: pin the run")

    verdict = check_commit(repo)

    assert verdict.ok is False
    assert "bernstein.yaml" in verdict.details


def test_gate_names_every_run_configuration_path_in_the_change(repo: Path) -> None:
    """Two leaked files produce two names, not just the first."""
    (repo / "bernstein.yaml").write_text(COMMITTED_SEED + "cells: 2\n", encoding="utf-8")
    (repo / ".claude").mkdir()
    (repo / ".claude" / "mcp.json").write_text("{}\n", encoding="utf-8")
    _git(repo, "add", "-A", "--force")

    verdict = check_staged(repo)

    assert verdict.ok is False
    assert set(verdict.offending_paths) == {"bernstein.yaml", ".claude/mcp.json"}
    for path in (".claude/mcp.json", "bernstein.yaml"):
        assert path in verdict.details


def test_gate_passes_a_change_that_touches_only_deliverables(repo: Path) -> None:
    """The gate must not stand in the way of the work the run was asked to do."""
    (repo / "src" / "feature.py").write_text("VALUE = 3\n", encoding="utf-8")
    _git(repo, "add", "-A")

    assert check_staged(repo).ok is True


def test_gate_ignores_a_same_named_file_at_a_nested_path(repo: Path) -> None:
    """``docs/bernstein.yaml`` is a deliverable, not the run's configuration."""
    (repo / "docs").mkdir()
    (repo / "docs" / "bernstein.yaml").write_text("goal: example\n", encoding="utf-8")
    _git(repo, "add", "-A")

    assert check_staged(repo).ok is True


def test_gate_fails_closed_when_the_diff_cannot_be_read(tmp_path: Path) -> None:
    """An unverifiable change is not a clean bill of health."""
    verdict = check_staged(tmp_path)

    assert verdict.ok is False
    assert verdict.offending_paths == (UNREADABLE,)


def test_gate_normalises_leading_dot_slash_without_eating_dotfiles(repo: Path) -> None:
    """``./bernstein.yaml`` matches; ``.bernstein/bernstein.yaml`` is not mangled."""
    assert check_paths(["./bernstein.yaml"]).ok is False
    assert check_paths([".bernstein/bernstein.yaml"]).ok is False
    assert check_paths(["bernstein/bernstein.yaml"]).ok is True


def test_run_config_gate_is_registered_and_required_by_default(repo: Path) -> None:
    """The invariant ships on, with the existing quality gates."""
    steps = build_default_pipeline(QualityGatesConfig())

    matching = [step for step in steps if step.name == "run_config"]
    assert len(matching) == 1, "run_config must be part of the default pipeline exactly once"
    assert matching[0].required is True, "a leak must block, not warn"


def test_merge_preflight_deny_list_covers_every_run_configuration_path() -> None:
    """The merge guard and the commit gate share one definition, so they cannot drift."""
    assert RUN_CONFIG_PATHS <= git_pr._MERGE_DENY_EXACT
    for path in RUN_CONFIG_PATHS:
        assert git_pr._is_forbidden_for_merge(path) is True


# ---------------------------------------------------------------------------
# The one file that must live in the work tree
# ---------------------------------------------------------------------------


def test_registered_run_exclude_keeps_the_bridge_manifest_out_of_a_broad_add(repo: Path) -> None:
    """``.claude/mcp.json`` is written in-tree but cannot be staged."""
    (repo / ".claude").mkdir()
    (repo / ".claude" / "mcp.json").write_text('{"mcpServers": {}}\n', encoding="utf-8")
    (repo / "src" / "feature.py").write_text("VALUE = 4\n", encoding="utf-8")

    added = register_run_excludes(repo)

    assert added == RUN_EXCLUDE_ENTRIES
    _git(repo, "add", "-A")
    staged = _staged(repo)
    assert ".claude/mcp.json" not in staged, "the bridge manifest must never be staged by a broad add"
    assert "src/feature.py" in staged, "the agent's real work must still be staged"


def test_run_excludes_leave_the_work_tree_reported_clean(repo: Path) -> None:
    """Writing the manifest plus its exclusion produces nothing for git to report."""
    (repo / ".claude").mkdir()
    (repo / ".claude" / "mcp.json").write_text('{"mcpServers": {}}\n', encoding="utf-8")
    register_run_excludes(repo)

    assert _git(repo, "status", "--porcelain") == ""


def test_registering_run_excludes_is_idempotent(repo: Path) -> None:
    """A second run does not append the same entry again."""
    first = register_run_excludes(repo)
    second = register_run_excludes(repo)

    assert first == RUN_EXCLUDE_ENTRIES
    assert second == ()
    exclude_text = (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert exclude_text.count(RUN_EXCLUDE_ENTRIES[0]) == 1


def test_run_excludes_do_not_hide_a_nested_deliverable_of_the_same_name(repo: Path) -> None:
    """The entries are anchored, so a project's own nested file is unaffected."""
    register_run_excludes(repo)
    nested = repo / "packages" / "app" / ".claude"
    nested.mkdir(parents=True)
    (nested / "mcp.json").write_text("{}\n", encoding="utf-8")

    _git(repo, "add", "-A")

    assert "packages/app/.claude/mcp.json" in _staged(repo)


# ---------------------------------------------------------------------------
# Bulk staging
# ---------------------------------------------------------------------------


def test_bulk_staging_unstages_run_configuration_not_only_directories(repo: Path) -> None:
    """``stage_all_except`` used to drop only the entries ending in ``/``."""
    (repo / "bernstein.yaml").write_text(COMMITTED_SEED.replace("max_agents: 3", "max_agents: 42"), encoding="utf-8")
    (repo / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (repo / "src" / "feature.py").write_text("VALUE = 5\n", encoding="utf-8")

    stage_all_except(repo)

    staged = _staged(repo)
    assert "src/feature.py" in staged, "real work must still be staged"
    assert "bernstein.yaml" not in staged
    assert ".env" not in staged
    assert check_staged(repo).ok is True


# ---------------------------------------------------------------------------
# The one path that pushes straight to the default branch
# ---------------------------------------------------------------------------


def test_evolve_refuses_to_push_a_commit_carrying_run_configuration(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate stops the push even where the staging filter does not reach.

    ``stage_all_except`` unstages the never-stage names, but that list is not
    the run-configuration set: ``.bernstein/bernstein.yaml`` is a seed file the
    loader reads and the staging filter has never heard of. The commit gate is
    what refuses the push.
    """
    import bernstein.core.git_ops as git_ops

    from bernstein.core.orchestration.orchestrator_evolve import evolve_auto_commit

    (repo / ".bernstein").mkdir()
    (repo / ".bernstein" / "bernstein.yaml").write_text(COMMITTED_SEED, encoding="utf-8")
    (repo / "src" / "feature.py").write_text("VALUE = 6\n", encoding="utf-8")

    real_run = subprocess.run

    def _run(cmd: list[str], **kwargs: Any) -> Any:
        if cmd[:3] == ["uv", "run", "pytest"]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return real_run(cmd, **kwargs)

    pushes: list[str] = []
    monkeypatch.setattr(git_ops, "safe_push", lambda _cwd, branch: pushes.append(branch))
    monkeypatch.setattr(subprocess, "run", _run)

    orch = SimpleNamespace(_workdir=repo)
    pushed = evolve_auto_commit(orch)

    assert pushed is False
    assert pushes == [], "a commit carrying run configuration must not reach the default branch"
    assert check_commit(repo).ok is False
