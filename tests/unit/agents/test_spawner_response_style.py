"""Spawn-path wiring tests for response-style profiles (issue #2243).

Verifies that ``spawn_for_tasks`` resolves the style deterministically,
hands the rendered addendum to the adapter via ``system_addendum``, stamps
the profile and its content hash onto the session and task metadata for
the cost ledger, and - critically - that a spawn with NO profile set is
byte-identical to a pre-change spawn (golden-prompt regression, AC3).
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from bernstein.core.agents.response_style import (
    addendum_sha256,
    render_style_addendum,
)
from bernstein.core.agents.spawner_core import AgentSpawner

if TYPE_CHECKING:
    from pathlib import Path

    from unittest.mock import MagicMock


def _build_spawner(
    tmp_path: Path,
    adapter: MagicMock,
    *,
    role_model_policy: dict[str, dict[str, Any]] | None = None,
) -> AgentSpawner:
    templates_dir = tmp_path / "templates" / "roles"
    templates_dir.mkdir(parents=True, exist_ok=True)
    return AgentSpawner(
        adapter,
        templates_dir,
        tmp_path,
        use_worktrees=False,
        default_model="mock-model",
        role_model_policy=role_model_policy,
    )


def _spawn_kwargs(adapter: MagicMock) -> dict[str, Any]:
    assert adapter.spawn.call_count == 1
    return adapter.spawn.call_args.kwargs


class TestGoldenPromptRegression:
    """AC3: a role with no style set behaves byte-identically to pre-change
    spawns - empty addendum, untouched prompt."""

    def test_no_profile_spawn_passes_empty_system_addendum(self, tmp_path, make_task, mock_adapter_factory) -> None:
        adapter = mock_adapter_factory()
        spawner = _build_spawner(tmp_path, adapter)
        spawner.spawn_for_tasks([make_task()])
        kwargs = _spawn_kwargs(adapter)
        # The pre-change spawner hardcoded system_addendum="" - byte-equal.
        assert kwargs["system_addendum"] == ""

    def test_no_profile_prompt_matches_profiled_spawn_prompt(self, tmp_path, make_task, mock_adapter_factory) -> None:
        """The addendum travels ONLY via ``system_addendum``: the rendered
        prompt of a terse spawn is byte-identical to a no-profile spawn of
        the same task (modulo the per-spawn session id and workdir)."""
        workdir_plain = tmp_path / "plain"
        workdir_terse = tmp_path / "terse"
        workdir_plain.mkdir()
        workdir_terse.mkdir()

        adapter_plain = mock_adapter_factory()
        spawner_plain = _build_spawner(workdir_plain, adapter_plain)
        session_plain = spawner_plain.spawn_for_tasks([make_task()])
        prompt_plain = _spawn_kwargs(adapter_plain)["prompt"]

        adapter_terse = mock_adapter_factory()
        spawner_terse = _build_spawner(
            workdir_terse,
            adapter_terse,
            role_model_policy={"backend": {"model": "mock-model", "response_style": "terse"}},
        )
        session_terse = spawner_terse.spawn_for_tasks([make_task()])
        prompt_terse = _spawn_kwargs(adapter_terse)["prompt"]

        def _normalize(prompt: str, session_id: str, workdir: Path) -> str:
            return prompt.replace(session_id, "SESSION").replace(str(workdir), "WORKDIR")

        assert _normalize(prompt_plain, session_plain.id, workdir_plain) == _normalize(
            prompt_terse, session_terse.id, workdir_terse
        )

    def test_no_profile_spawn_still_records_balanced_profile(self, tmp_path, make_task, mock_adapter_factory) -> None:
        """Every spawn carries a declared profile - unset resolves to the
        neutral ``balanced`` and the hash of the empty addendum."""
        adapter = mock_adapter_factory()
        spawner = _build_spawner(tmp_path, adapter)
        task = make_task()
        session = spawner.spawn_for_tasks([task])
        assert session.response_profile == "balanced"
        assert session.profile_content_sha256 == hashlib.sha256(b"").hexdigest()
        assert task.metadata["response_profile"] == "balanced"
        assert task.metadata["profile_content_sha256"] == hashlib.sha256(b"").hexdigest()


class TestProfiledSpawn:
    def test_role_policy_terse_flows_into_system_addendum(self, tmp_path, make_task, mock_adapter_factory) -> None:
        adapter = mock_adapter_factory()
        spawner = _build_spawner(
            tmp_path,
            adapter,
            role_model_policy={"backend": {"model": "mock-model", "response_style": "terse"}},
        )
        task = make_task()
        session = spawner.spawn_for_tasks([task])

        expected = render_style_addendum("terse", workdir=tmp_path)
        assert expected != ""
        kwargs = _spawn_kwargs(adapter)
        assert kwargs["system_addendum"] == expected
        # AC2: ledger coupling inputs are stamped on session + task metadata.
        assert session.response_profile == "terse"
        assert session.profile_content_sha256 == addendum_sha256(expected)
        assert task.metadata["response_profile"] == "terse"
        assert task.metadata["profile_content_sha256"] == addendum_sha256(expected)

    def test_task_metadata_mode_overrides_role_policy(self, tmp_path, make_task, mock_adapter_factory) -> None:
        adapter = mock_adapter_factory()
        spawner = _build_spawner(
            tmp_path,
            adapter,
            role_model_policy={"backend": {"model": "mock-model", "response_style": "terse"}},
        )
        task = make_task()
        task.metadata["mode"] = "verbose"
        session = spawner.spawn_for_tasks([task])
        expected = render_style_addendum("verbose", workdir=tmp_path)
        assert _spawn_kwargs(adapter)["system_addendum"] == expected
        assert session.response_profile == "verbose"

    def test_seed_default_entry_supplies_style(self, tmp_path, make_task, mock_adapter_factory) -> None:
        adapter = mock_adapter_factory()
        spawner = _build_spawner(
            tmp_path,
            adapter,
            role_model_policy={
                "default": {"model": "mock-model", "response_style": "terse"},
                "backend": {"model": "mock-model"},
            },
        )
        session = spawner.spawn_for_tasks([make_task()])
        assert session.response_profile == "terse"

    def test_rendered_addendum_is_deterministic_across_spawns(self, tmp_path, make_task, mock_adapter_factory) -> None:
        """AC1 at the spawn seam: two spawns with identical config and task
        metadata hand the adapter byte-identical addenda."""
        addenda: list[str] = []
        for _ in range(2):
            adapter = mock_adapter_factory()
            spawner = _build_spawner(
                tmp_path,
                adapter,
                role_model_policy={"backend": {"model": "mock-model", "response_style": "verbose"}},
            )
            spawner.spawn_for_tasks([make_task()])
            addenda.append(_spawn_kwargs(adapter)["system_addendum"])
        assert addenda[0] == addenda[1]
        assert addenda[0].startswith("## Response style: verbose\n")
