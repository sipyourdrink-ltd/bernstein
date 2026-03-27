"""Test that CLI decomposition works -- all imports are available after refactoring."""

from __future__ import annotations


def test_task_cmd_imports() -> None:
    """Test that task command modules can be imported."""
    # Should not raise ImportError
    from bernstein.cli import task_cmd

    assert hasattr(task_cmd, "cancel")
    assert hasattr(task_cmd, "add_task")
    assert hasattr(task_cmd, "approve")
    assert hasattr(task_cmd, "reject")
    assert hasattr(task_cmd, "pending")
    assert hasattr(task_cmd, "review_cmd")
    assert hasattr(task_cmd, "list_tasks")
    assert hasattr(task_cmd, "sync")


def test_workspace_cmd_imports() -> None:
    """Test that workspace command modules can be imported."""
    from bernstein.cli import workspace_cmd

    assert hasattr(workspace_cmd, "workspace_group")
    assert hasattr(workspace_cmd, "config_group")
    assert hasattr(workspace_cmd, "plan")


def test_advanced_cmd_imports() -> None:
    """Test that advanced command modules can be imported."""
    from bernstein.cli import advanced_cmd

    assert hasattr(advanced_cmd, "trace_cmd")
    assert hasattr(advanced_cmd, "replay_cmd")
    assert hasattr(advanced_cmd, "github_group")
    assert hasattr(advanced_cmd, "mcp_server")
    assert hasattr(advanced_cmd, "quarantine_group")
    assert hasattr(advanced_cmd, "completions")
    assert hasattr(advanced_cmd, "live")
    assert hasattr(advanced_cmd, "dashboard")
    assert hasattr(advanced_cmd, "ideate")
    assert hasattr(advanced_cmd, "install_hooks")
    assert hasattr(advanced_cmd, "plugins_cmd")
    assert hasattr(advanced_cmd, "doctor")
    assert hasattr(advanced_cmd, "recap")
    assert hasattr(advanced_cmd, "help_all")
    assert hasattr(advanced_cmd, "retro")


def test_eval_benchmark_cmd_imports() -> None:
    """Test that eval/benchmark command modules can be imported."""
    from bernstein.cli import eval_benchmark_cmd

    assert hasattr(eval_benchmark_cmd, "benchmark_group")
    assert hasattr(eval_benchmark_cmd, "eval_group")


def test_backward_compat_main_imports() -> None:
    """Test that all commands are still available from bernstein.cli.main (backward compat)."""
    from bernstein.cli.main import (
        add_task,
        approve,
        benchmark_group,
        cancel,
        completions,
        config_group,
        dashboard,
        doctor,
        eval_group,
        github_group,
        help_all,
        ideate,
        install_hooks,
        list_tasks,
        live,
        logs_cmd,
        mcp_server,
        pending,
        plan,
        plugins_cmd,
        quarantine_group,
        recap,
        reject,
        replay_cmd,
        retro,
        review_cmd,
        sync,
        trace_cmd,
        workspace_group,
    )

    # Just importing should work
    assert cancel is not None
    assert add_task is not None
    assert approve is not None
    assert reject is not None
    assert pending is not None
    assert review_cmd is not None
    assert list_tasks is not None
    assert sync is not None
    assert plan is not None
    assert workspace_group is not None
    assert config_group is not None
    assert trace_cmd is not None
    assert replay_cmd is not None
    assert github_group is not None
    assert mcp_server is not None
    assert benchmark_group is not None  # Re-exported from eval_benchmark_cmd
    assert eval_group is not None  # Re-exported from eval_benchmark_cmd
    assert quarantine_group is not None
    assert completions is not None
    assert logs_cmd is not None
    assert live is not None
    assert dashboard is not None
    assert ideate is not None
    assert install_hooks is not None
    assert plugins_cmd is not None
    assert doctor is not None
    assert recap is not None
    assert help_all is not None
    assert retro is not None
