"""Regression tests for the manager → task-server auth flow (#1261).

Covers:

* :func:`_render_auth_section` always emits an **absolute** token file path
  so the agent's ``cat $TOKEN_PATH`` resolves to the real token regardless
  of the agent's spawn cwd (project root vs git worktree).
* :meth:`AgentSpawner._issue_agent_token` writes the token to an absolute
  path under the orchestrator workdir, even when the spawner is
  constructed with a relative ``workdir`` argument.
* The auth section instructions reference the absolute path AND the
  ``BERNSTEIN_AUTH_TOKEN`` env fallback so the agent has two independent
  ways to reach the task server.

The original bug (#1261): the manager agent runs in
``.sdd/worktrees/<session>/`` (cwd ≠ orchestrator project root); the
auth section embedded a path that, depending on caller arguments, could
end up relative; the agent's ``cat <relative>`` resolved against the
worktree, missed the real token in the project root's
``.sdd/runtime/agent_tokens/``, and every ``POST /tasks`` returned 401.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from bernstein.core.models import ModelConfig

from bernstein.adapters.base import CLIAdapter, SpawnResult
from bernstein.core.agents.spawner_core import (
    AgentSpawner,
    _render_auth_section,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _NoopAdapter(CLIAdapter):
    """Stub adapter - never spawns a process; just satisfies the constructor."""

    def spawn(
        self,
        *,
        prompt: str,
        workdir: Path,
        model_config: ModelConfig,
        session_id: str,
        mcp_config: dict[str, Any] | None = None,
        timeout_seconds: int = 1800,
        task_scope: str = "medium",
        budget_multiplier: float = 1.0,
        system_addendum: str = "",
    ) -> SpawnResult:
        raise NotImplementedError

    def name(self) -> str:
        return "noop"


def _make_spawner(workdir: Path) -> AgentSpawner:
    """Build an AgentSpawner with worktrees disabled (fast, deterministic)."""
    templates_dir = workdir / "templates"
    templates_dir.mkdir(parents=True, exist_ok=True)
    return AgentSpawner(
        adapter=_NoopAdapter(),
        templates_dir=templates_dir,
        workdir=workdir,
        use_worktrees=False,
    )


# ---------------------------------------------------------------------------
# _render_auth_section - path absolutisation
# ---------------------------------------------------------------------------


def test_render_auth_section_absolutises_relative_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A relative ``token_path`` must be rewritten to absolute in the prompt.

    Without this guarantee the agent's ``cat`` command resolves the path
    against its spawn cwd (the worktree) instead of the orchestrator
    workdir, and authentication silently breaks.
    """
    # cwd is set to tmp_path so we can construct a relative path that
    # resolves under tmp_path/.sdd/...
    monkey_cwd = tmp_path
    monkeypatch.chdir(monkey_cwd)
    relative = Path(".sdd/runtime/agent_tokens/manager-abc12345.token")
    assert not relative.is_absolute()

    section = _render_auth_section(relative)

    # The rendered block must contain the absolute resolution of the path,
    # not the relative form.
    expected_abs = relative.resolve(strict=False)
    assert str(expected_abs) in section
    assert "## Task Server Authentication" in section
    # The instruction text must mention the absolute path quality so the
    # agent does not assume cwd-relative addressing.
    assert "absolute path" in section.lower()


def test_render_auth_section_preserves_absolute_path(tmp_path: Path) -> None:
    """An already-absolute path is passed through verbatim."""
    absolute = (tmp_path / ".sdd" / "runtime" / "agent_tokens" / "manager-deadbeef.token").resolve()
    section = _render_auth_section(absolute)
    assert str(absolute) in section


def test_render_auth_section_mentions_env_fallback(tmp_path: Path) -> None:
    """The auth section advertises ``BERNSTEIN_AUTH_TOKEN`` as a fallback.

    Even if the token file is unreadable (filesystem race, permission
    change, missing parent dir) the agent must have a documented second
    channel - env var inheritance via the env_isolation allowlist.
    """
    section = _render_auth_section(tmp_path / "token")
    assert "BERNSTEIN_AUTH_TOKEN" in section


def test_render_auth_section_includes_authorization_header_example(tmp_path: Path) -> None:
    """The section must show a curl example with the Authorization header."""
    section = _render_auth_section(tmp_path / "manager.token")
    assert "Authorization: Bearer $(cat" in section
    assert "POST http://127.0.0.1:8052/tasks" in section


# ---------------------------------------------------------------------------
# _issue_agent_token - writes the token file at an absolute path
# ---------------------------------------------------------------------------


def test_issue_agent_token_returns_absolute_path(tmp_path: Path, monkeypatch: Any) -> None:
    """Even when the spawner workdir is relative, the token path is absolute.

    Regression for #1261: a relative workdir → relative token_path →
    agent's ``cat`` (executed inside the worktree) misses the file.
    """
    # Construct the spawner with a relative workdir. We chdir to the
    # parent so the relative path is well-defined.
    monkeypatch.chdir(tmp_path)
    relative_workdir = Path("project-root")
    relative_workdir.mkdir()

    spawner = _make_spawner(relative_workdir)

    # Stub out identity-store credential issuance - we are not testing JWT
    # signing here, just path handling.
    stub_identity = MagicMock()
    stub_identity.create_identity = MagicMock(return_value=(MagicMock(), "fake-jwt-token-body"))
    spawner._identity_store_instance = stub_identity

    token_path = spawner._issue_agent_token(
        session_id="manager-abcd1234",
        role="manager",
        task_ids=["T-001"],
    )

    assert token_path.is_absolute(), f"Token path must be absolute, got {token_path}"
    assert token_path.exists()
    # The file lives under the resolved workdir, not the cwd-relative form.
    expected_root = relative_workdir.resolve() / ".sdd" / "runtime" / "agent_tokens"
    assert token_path.parent == expected_root
    assert token_path.read_text() == "fake-jwt-token-body"


def test_issue_agent_token_file_mode_is_0600(tmp_path: Path) -> None:
    """The token file is written with 0600 permissions (owner read/write only).

    Defence-in-depth: even with the absolute-path fix, a world-readable
    token file would leak credentials to other local users.
    """
    if os.name == "nt":  # pragma: no cover - POSIX-only file modes
        return

    spawner = _make_spawner(tmp_path)
    spawner._identity_store_instance = MagicMock(
        create_identity=MagicMock(return_value=(MagicMock(), "secret-jwt")),
    )

    token_path = spawner._issue_agent_token(
        session_id="manager-aaaabbbb",
        role="manager",
        task_ids=[],
    )

    mode = token_path.stat().st_mode & 0o777
    assert mode == 0o600, f"Expected 0600, got {oct(mode)}"


def test_auth_section_in_prompt_resolves_when_agent_cwd_is_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: prompt-embedded path resolves to a real file from a worktree.

    Simulates the manager agent's runtime: ``cat $TOKEN_PATH`` is
    invoked with cwd set to ``.sdd/worktrees/<session>/`` (a subdir of
    the orchestrator workdir). With the #1261 fix the absolute path
    survives the cwd change and the token file is readable.
    """
    project_root = tmp_path / "project"
    project_root.mkdir()
    spawner = _make_spawner(project_root)
    spawner._identity_store_instance = MagicMock(
        create_identity=MagicMock(return_value=(MagicMock(), "live-token-bytes")),
    )

    token_path = spawner._issue_agent_token(
        session_id="manager-ccccdddd",
        role="manager",
        task_ids=[],
    )
    section = _render_auth_section(token_path)

    # Extract the path from the rendered section the same way an agent
    # would parse it - the absolute path appears in the fenced block.
    assert str(token_path) in section
    assert token_path.is_absolute()

    # Now simulate the worktree cwd: change to a subdir and verify the
    # absolute path still reads back the token.
    worktree_dir = project_root / ".sdd" / "worktrees" / "manager-ccccdddd"
    worktree_dir.mkdir(parents=True)
    monkeypatch.chdir(worktree_dir)
    assert token_path.read_text() == "live-token-bytes"
