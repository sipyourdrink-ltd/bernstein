"""Heterogeneous-fleet delivery of mailbox messages (#2357, AC4).

The typed rendering of pending mailbox messages is injected into the
worker's task context by the shared prompt build - before any adapter is
chosen - so two different adapter types receive the byte-identical
coordination section. The test drives two real adapter spawn paths
(claude and codex CLIs) with mocked subprocesses and compares the
section each adapter actually passed to its process.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.models import ModelConfig

from bernstein.adapters.claude import ClaudeCodeAdapter
from bernstein.adapters.codex import CodexAdapter
from bernstein.core.agents.spawn_prompt import render_prompt
from bernstein.core.communication.task_mailbox import TaskMailbox, render_mailbox_section
from tests.factories import make_task

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.usefixtures("no_watchdog_threads")

_KEY = b"fleet-test-key"


def _popen_mock(pid: int) -> MagicMock:
    m = MagicMock(spec=subprocess.Popen)
    m.pid = pid
    m.stdout = MagicMock()
    return m


def _prompt_with_mailbox(tmp_path: Path) -> tuple[str, str]:
    """Return ``(prompt, rendered_section)`` for a task with pending mail."""
    mailbox = TaskMailbox(
        tmp_path / ".sdd" / "runtime" / "mailbox.jsonl",
        hmac_key=_KEY,
        identity_dir=tmp_path / ".sdd" / "identity",
    )
    task = make_task(title="Fix error mapping", role="backend")
    mailbox.post(
        task_id=task.id,
        sender="reviewer-1",
        kind="finding",
        body="The retry helper is duplicated; reuse core/retry.",
    )
    mailbox.post(
        task_id=task.id,
        sender="planner",
        kind="question",
        body="Is the v2 schema frozen?",
    )
    section = render_mailbox_section(mailbox.pending(task.id))
    assert section  # non-empty rendering for pending messages
    prompt = render_prompt(
        tasks=[task],
        templates_dir=tmp_path / "templates",
        workdir=tmp_path,
        mailbox_section=section,
    )
    return prompt, section


def _spawn_claude(prompt: str, workdir: Path) -> str:
    adapter = ClaudeCodeAdapter()
    with patch(
        "bernstein.adapters.claude.subprocess.Popen",
        side_effect=[_popen_mock(300), _popen_mock(301)],
    ) as popen:
        adapter.spawn(
            prompt=prompt,
            workdir=workdir,
            model_config=ModelConfig(model="sonnet", effort="low"),
            session_id="backend-claude1",
            timeout_seconds=0,
        )
        argv: list[str] = list(popen.call_args_list[0].args[0])
    return argv[argv.index("-p") + 1]


def _spawn_codex(prompt: str, workdir: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
    adapter = CodexAdapter()
    with patch(
        "bernstein.adapters.codex.subprocess.Popen",
        return_value=_popen_mock(302),
    ) as popen:
        adapter.spawn(
            prompt=prompt,
            workdir=workdir,
            model_config=ModelConfig(model="gpt-5.5", effort="low"),
            session_id="backend-codex1",
            timeout_seconds=0,
        )
        argv = list(popen.call_args_list[0].args[0])
    return str(argv[-1])


def test_prompt_contains_typed_mailbox_rendering(tmp_path: Path) -> None:
    prompt, section = _prompt_with_mailbox(tmp_path)
    assert section.strip() in prompt
    assert "finding" in prompt
    assert "reviewer-1" in prompt
    assert "core/retry" in prompt


def test_two_adapter_types_receive_identical_mailbox_section(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prompt, section = _prompt_with_mailbox(tmp_path)

    claude_workdir = tmp_path / "claude-wt"
    codex_workdir = tmp_path / "codex-wt"
    claude_workdir.mkdir()
    codex_workdir.mkdir()

    claude_prompt = _spawn_claude(prompt, claude_workdir)
    codex_prompt = _spawn_codex(prompt, codex_workdir, monkeypatch)

    marker = section.strip()
    assert marker in claude_prompt
    assert marker in codex_prompt

    def _extract(text: str) -> str:
        start = text.index(marker)
        return text[start : start + len(marker)]

    # Byte-identical typed rendering across two different adapter types.
    assert _extract(claude_prompt) == _extract(codex_prompt) == marker


def test_mailbox_section_absent_when_no_pending_messages(tmp_path: Path) -> None:
    task = make_task(title="No mail", role="backend")
    mailbox = TaskMailbox(tmp_path / ".sdd" / "runtime" / "mailbox.jsonl")
    section = render_mailbox_section(mailbox.pending(task.id))
    assert section == ""
    prompt = render_prompt(
        tasks=[task],
        templates_dir=tmp_path / "templates",
        workdir=tmp_path,
        mailbox_section=section,
    )
    assert "Coordination mailbox" not in prompt
