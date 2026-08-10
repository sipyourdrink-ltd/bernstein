"""Unit tests for GooseAdapter's constructed argv.

No test file previously existed for this adapter (verified via
``find tests -iname '*goose*'`` before writing this file). Pattern matched
from ``test_adapter_ollama.py``.

These tests pin the *invocation surface* only, so they run without the
``goose`` binary present.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from bernstein.core.models import ModelConfig

from bernstein.adapters.goose import GooseAdapter
from tests.unit._adapter_test_helpers import inner_cmd, make_popen_mock

if TYPE_CHECKING:
    from pathlib import Path


pytestmark = pytest.mark.usefixtures("no_watchdog_threads")


class TestGooseAdapterSpawn:
    """GooseAdapter.spawn() builds the expected ``goose run`` command."""

    def _spawn(self, tmp_path: Path, *, model: str = "sonnet", pid: int = 900) -> list[str]:
        adapter = GooseAdapter()
        proc_mock = make_popen_mock(pid=pid)
        with patch("bernstein.adapters.goose.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="fix the bug",
                workdir=tmp_path,
                model_config=ModelConfig(model=model, effort="high"),
                session_id="goose-s1",
            )
        return inner_cmd(popen.call_args.args[0])

    def test_uses_run_subcommand(self, tmp_path: Path) -> None:
        inner = self._spawn(tmp_path)
        assert inner[0] == "goose"
        assert inner[1] == "run"

    def test_prompt_passed_inline_via_text_flag(self, tmp_path: Path) -> None:
        """The inline prompt must ride ``--text``.

        ``goose run`` takes the prompt inline only via ``--text/-t``.
        """
        inner = self._spawn(tmp_path)
        assert "--text" in inner
        assert inner[inner.index("--text") + 1] == "fix the bug"

    def test_never_uses_removed_instruction_flag(self, tmp_path: Path) -> None:
        """Regression guard: ``--instruction`` is not a goose flag.

        Upstream exposes ``--instructions <FILE>`` (a path, ``-`` for stdin)
        and ``--text <TEXT>``; the singular ``--instruction`` does not exist,
        so driving it aborts the run.
        """
        inner = self._spawn(tmp_path)
        assert "--instruction" not in inner

    def test_prompt_is_not_passed_as_a_file_path(self, tmp_path: Path) -> None:
        """``--instructions`` takes a FILE, so an inline prompt must not use it."""
        inner = self._spawn(tmp_path)
        assert "--instructions" not in inner

    def test_model_flag_passthrough(self, tmp_path: Path) -> None:
        inner = self._spawn(tmp_path, model="sonnet", pid=901)
        assert "--model" in inner
        assert inner[inner.index("--model") + 1] == "claude-sonnet-4-6"
