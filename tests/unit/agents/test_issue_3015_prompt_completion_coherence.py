"""Issue #3015 - a single, coherent completion instruction per role.

The fix routes task completion through the first-class ``bernstein task
complete`` CLI. It must not leave a role's prompt carrying TWO conflicting
completion instructions (a CLI line *and* a raw-curl ``/complete`` POST) - that
would reproduce, or worsen, the quoting/decision confusion #3015 is about.

These tests assemble the **live** agent prompt exactly as production does
(``spawner_core._render_prompt`` for the role body + the appended
``_render_auth_section``) and assert that every role - manager and workers -
sees the CLI and nothing that tells it to hand-build a completion curl.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from bernstein.core.models import Task

from bernstein import _BUNDLED_TEMPLATES_DIR
from bernstein.core.agents.spawner_core import _render_auth_section, _render_prompt

# curl POST to a /tasks/<id>/complete endpoint on a single line - the fragile
# shape the issue calls out. Matches the inline instructions block and the
# batch prompt whether or not they also carry the JSON body.
_RAW_CURL_COMPLETE = re.compile(r"curl[^\n]*/tasks/\S*?/complete", re.IGNORECASE)

_ROLES = ["manager", "backend", "qa", "security", "reviewer", "docs"]


def _live_prompt(role: str, tmp_path: Path) -> str:
    """Assemble the prompt for *role* the way the production spawner does."""
    workdir = tmp_path / role
    (workdir / ".sdd").mkdir(parents=True, exist_ok=True)
    roles_dir = _BUNDLED_TEMPLATES_DIR / "roles"
    task = Task(id="T-1", title="Do the thing", description="A task body.", role=role)
    body = _render_prompt([task], roles_dir, workdir)
    auth = _render_auth_section(workdir / ".sdd" / "runtime" / "agent_tokens" / "s.token")
    return body + auth


class TestPromptCompletionCoherence:
    @pytest.mark.parametrize("role", _ROLES)
    def test_role_prompt_has_single_cli_completion(self, role: str, tmp_path: Path) -> None:
        prompt = _live_prompt(role, tmp_path)
        # The one and only completion instruction is the CLI front door.
        assert "bernstein task complete" in prompt, f"{role}: CLI completion instruction missing"
        # No competing raw-curl completion anywhere in the same prompt.
        match = _RAW_CURL_COMPLETE.search(prompt)
        assert match is None, f"{role}: raw-curl completion still present -> {match and match.group(0)!r}"

    @pytest.mark.parametrize("role", _ROLES)
    def test_role_prompt_has_no_result_summary_json_body(self, role: str, tmp_path: Path) -> None:
        """No hand-quoted JSON completion body should remain in the live prompt."""
        prompt = _live_prompt(role, tmp_path)
        assert '"result_summary"' not in prompt, f"{role}: hand-quoted result_summary JSON body still present"
