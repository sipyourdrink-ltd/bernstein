"""Tests for manager-role consensus relay injection into spawn prompts (issue #4678)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

from bernstein.core.spawn_prompt import _render_prompt


class TestManagerRelaySpawnPrompt:
    """Verify consensus relay content is injected into manager spawn prompts and absent elsewhere."""

    def test_manager_prompt_contains_consensus_section(self, tmp_path: Path, make_task: Any) -> None:
        """A manager-role spawn prompt includes the consensus section from build_spawn_section."""
        task = make_task(id="T-1", role="manager", title="Plan next iteration")
        templates_dir = tmp_path / "templates"
        templates_dir.mkdir()

        consensus_block = (
            "## Prior cycle consensus\n"
            "- phase: plan\n\n"
            "### Decisions\n\n"
            "- **use postgres** (confidence 0.90)\n"
            "  - scales better\n\n"
            "### Open questions\n\n"
            "- is the timeout right?\n\n"
            "### Next action\n\n"
            "confirm with team\n"
        )

        with patch(
            "bernstein.core.orchestration.consensus_relay.build_spawn_section",
            return_value=consensus_block,
        ):
            prompt = _render_prompt(
                [task],
                templates_dir=templates_dir,
                workdir=tmp_path,
                session_id="mgr-1",
            )

        assert "## Prior cycle consensus" in prompt
        assert "use postgres" in prompt
        assert "is the timeout right?" in prompt
        assert "confirm with team" in prompt

    def test_non_manager_roles_unchanged(self, tmp_path: Path, make_task: Any) -> None:
        """Backend/frontend/qa prompts do NOT contain the consensus section."""
        task = make_task(id="T-2", role="backend", title="Implement feature X")
        templates_dir = tmp_path / "templates"
        templates_dir.mkdir()

        with patch(
            "bernstein.core.orchestration.consensus_relay.build_spawn_section",
            return_value="## Prior cycle consensus\ninjected content\n",
        ):
            prompt = _render_prompt(
                [task],
                templates_dir=templates_dir,
                workdir=tmp_path,
                session_id="be-1",
            )

        assert "Prior cycle consensus" not in prompt
        assert "injected content" not in prompt

    def test_size_cap_enforced(self, tmp_path: Path) -> None:
        """The consensus section is capped at 4000 bytes by build_spawn_section."""
        # The cap is enforced in build_spawn_section itself (TestBuildSpawnSection
        # covers that directly). Here we verify the cap is respected when the
        # spawner calls build_spawn_section with real oversized data.
        from bernstein.core.orchestration.consensus_relay import (
            RelayDecision,
            RelayStore,
            build_spawn_section,
        )

        relay_dir = tmp_path / ".sdd" / "runtime" / "consensus"
        relay_dir.mkdir(parents=True)
        store = RelayStore(relay_dir, key=b"k" * 32)
        store.append(
            cycle_id="c1",
            phase="implement",
            decisions=tuple(
                RelayDecision(title=f"decision {i}", rationale="x" * 500, confidence=0.5) for i in range(20)
            ),
            open_questions=tuple(f"q{i}: " + "x" * 200 for i in range(10)),
            next_action="y" * 2000,
        )

        section = build_spawn_section(relay_root=relay_dir, key=b"k" * 32)
        assert len(section.encode("utf-8")) <= 4000, (
            f"Consensus section too large: {len(section.encode('utf-8'))} bytes"
        )

    def test_deterministic_render(self, tmp_path: Path, make_task: Any) -> None:
        """Two renders of the same manager task with the same relay store produce identical prompts."""
        task = make_task(id="T-4", role="manager", title="Plan next iteration")
        templates_dir = tmp_path / "templates"
        templates_dir.mkdir()

        consensus_block = (
            "## Prior cycle consensus\n"
            "- phase: implement\n\n"
            "### Decisions\n\n"
            "- **go left** (confidence 0.70)\n"
            "  - because\n\n"
            "### Open questions\n\n"
            "- is this safe?\n\n"
            "### Next action\n\n"
            "ship it\n"
        )

        with patch(
            "bernstein.core.orchestration.consensus_relay.build_spawn_section",
            return_value=consensus_block,
        ):
            first = _render_prompt(
                [task],
                templates_dir=templates_dir,
                workdir=tmp_path,
                session_id="mgr-3",
            )
            second = _render_prompt(
                [task],
                templates_dir=templates_dir,
                workdir=tmp_path,
                session_id="mgr-3",
            )

        assert first == second

    def test_empty_consensus_returns_empty_section(self, tmp_path: Path, make_task: Any) -> None:
        """When build_spawn_section returns empty string, the prompt contains no consensus section."""
        task = make_task(id="T-5", role="manager", title="Plan next iteration")
        templates_dir = tmp_path / "templates"
        templates_dir.mkdir()

        with patch(
            "bernstein.core.orchestration.consensus_relay.build_spawn_section",
            return_value="",
        ):
            prompt = _render_prompt(
                [task],
                templates_dir=templates_dir,
                workdir=tmp_path,
                session_id="mgr-4",
            )

        assert "Prior cycle consensus" not in prompt
