"""Issue #3015 - the manager prompt must not hand-build a completion curl.

The task-server auth section appended to every agent's prompt used to carry a
raw ``curl -X POST …/tasks/<id>/complete`` example with a Bearer header sourced
from ``$(cat …token)`` and a JSON ``-d '{"result_summary": …}'`` body - several
nested quoting layers inside one ``run_command`` string. Marking a task
complete is the load-bearing action, so expressing it as the most
quoting-hostile shape made the most important action the most fragile one.

The fix routes completion through the first-class ``bernstein task complete``
CLI. These tests assert the generated prompt text no longer instructs the
raw-curl completion, and that the manager templates point at the CLI instead.
"""

from __future__ import annotations

import re

import pytest

from bernstein import _BUNDLED_TEMPLATES_DIR
from bernstein.core.agents.spawner_core import _render_auth_section

# A raw-curl completion snippet: a curl POST to a ``/tasks/<id>/complete``
# endpoint that also hand-carries the JSON ``result_summary`` body. The gap is
# DOTALL + bounded so it matches whether the command is on one line (the
# manager templates) or spread over ``\``-continued lines (the auth section).
_RAW_CURL_COMPLETE = re.compile(
    r"curl.{0,600}?/tasks/\S*?/complete.{0,600}?result_summary",
    re.IGNORECASE | re.DOTALL,
)


class TestAuthSectionCompletion:
    def test_auth_section_has_no_raw_curl_completion(self) -> None:
        """The appended auth section must not carry a raw-curl completion example."""
        section = _render_auth_section(_dummy_token_path())
        assert not _RAW_CURL_COMPLETE.search(section), "auth section still instructs a raw-curl completion:\n" + section
        # Belt and braces: the completion endpoint and its JSON body field
        # should no longer appear in the auth section at all.
        assert "/complete" not in section
        assert "result_summary" not in section

    def test_auth_section_instructs_task_complete_cli(self) -> None:
        """It must instead point at the first-class ``bernstein task complete`` CLI."""
        section = _render_auth_section(_dummy_token_path())
        assert "bernstein task complete" in section

    def test_auth_section_still_documents_auth_header_and_subtask_post(self) -> None:
        """Non-completion server calls (creating subtasks) still need the header.

        The fix is scoped to completion; the Authorization guidance and the
        subtask-creation example must survive so the manager can still POST
        new tasks over HTTP.
        """
        section = _render_auth_section(_dummy_token_path())
        assert "Authorization: Bearer" in section
        assert "/tasks" in section


def _read_template(*parts: str) -> str:
    return (_BUNDLED_TEMPLATES_DIR.joinpath(*parts)).read_text(encoding="utf-8")


def _dummy_token_path():
    from pathlib import Path

    return Path("/tmp/agent-3015.token")


class TestManagerTemplatesCompletion:
    @pytest.mark.parametrize(
        "parts",
        [
            ("skills", "manager", "SKILL.md"),
            ("roles", "manager", "system_prompt.md"),
        ],
    )
    def test_manager_template_drops_raw_curl_completion(self, parts: tuple[str, ...]) -> None:
        """Neither manager prompt template hand-builds the completion curl."""
        text = _read_template(*parts)
        assert not _RAW_CURL_COMPLETE.search(text), f"{'/'.join(parts)} still instructs a raw-curl completion"
        assert "bernstein task complete" in text
