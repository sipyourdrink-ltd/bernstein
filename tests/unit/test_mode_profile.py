"""Tests for per-model agent mode profiles (smart/deep/fast)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from bernstein.core.routing.mode_profile import (
    MODE_REGISTRY,
    ModeProfile,
    apply_mode,
    install_loaded_profiles,
    load_profiles_from_dir,
    select_mode,
)


@dataclass
class _FakeTask:
    metadata: dict[str, Any] = field(default_factory=dict[str, Any])


class TestSelectMode:
    def test_claude_defaults_to_smart(self) -> None:
        profile = select_mode("claude-sonnet-4-6")
        assert profile.name == "smart"

    def test_opus_defaults_to_smart(self) -> None:
        assert select_mode("claude-opus-4-7").name == "smart"

    def test_haiku_defaults_to_fast(self) -> None:
        assert select_mode("claude-haiku-4").name == "fast"

    def test_gpt5_defaults_to_deep(self) -> None:
        profile = select_mode("gpt-5.2-pro")
        assert profile.name == "deep"

    def test_gpt5_has_longer_max_turns_than_smart(self) -> None:
        deep = select_mode("gpt-5.2")
        smart = select_mode("claude-sonnet-4-6")
        assert deep.max_turns > smart.max_turns

    def test_o3_defaults_to_deep(self) -> None:
        assert select_mode("o3-mini").name == "deep"

    def test_unknown_model_falls_back_to_smart(self) -> None:
        assert select_mode("unknown-model-xyz").name == "smart"

    def test_empty_model_id_falls_back_to_smart(self) -> None:
        assert select_mode("").name == "smart"

    def test_metadata_mode_overrides_default(self) -> None:
        task = _FakeTask(metadata={"mode": "fast"})
        profile = select_mode("claude-sonnet-4-6", task)
        assert profile.name == "fast"

    def test_metadata_mode_overrides_gpt5_default(self) -> None:
        task = _FakeTask(metadata={"mode": "smart"})
        assert select_mode("gpt-5.2-pro", task).name == "smart"

    def test_unknown_metadata_mode_falls_back_to_default(self) -> None:
        task = _FakeTask(metadata={"mode": "warp-speed"})
        assert select_mode("claude-sonnet-4-6", task).name == "smart"

    def test_task_without_metadata_attribute_is_safe(self) -> None:
        class _Bare: ...

        assert select_mode("claude-sonnet-4-6", _Bare()).name == "smart"


class TestModeProfileBehaviour:
    def test_filter_tools_with_empty_subset_returns_all(self) -> None:
        profile = ModeProfile(name="x", system_prompt_preamble="", tool_subset=())
        assert profile.filter_tools(["A", "B", "C"]) == ["A", "B", "C"]

    def test_filter_tools_intersects_with_subset(self) -> None:
        profile = ModeProfile(
            name="x",
            system_prompt_preamble="",
            tool_subset=("Read", "Edit"),
        )
        filtered = profile.filter_tools(["Read", "Bash", "Edit", "Write"])
        assert filtered == ["Read", "Edit"]

    def test_filter_tools_drops_disallowed(self) -> None:
        profile = ModeProfile(name="x", system_prompt_preamble="", tool_subset=("Read",))
        assert profile.filter_tools(["Bash", "Write"]) == []

    def test_apply_preamble_prepends_with_blank_line(self) -> None:
        profile = ModeProfile(name="x", system_prompt_preamble="HEADER", tool_subset=())
        result = profile.apply_preamble("body text")
        assert result.startswith("HEADER\n\n")
        assert result.endswith("body text")

    def test_apply_preamble_empty_returns_prompt_unchanged(self) -> None:
        profile = ModeProfile(name="x", system_prompt_preamble="", tool_subset=())
        assert profile.apply_preamble("body") == "body"


class TestApplyMode:
    def test_apply_mode_filters_tools_and_injects_preamble(self) -> None:
        profile = ModeProfile(
            name="fast",
            system_prompt_preamble="MODE: fast",
            tool_subset=("Read",),
            max_turns=10,
        )
        applied = apply_mode(profile, prompt="do the thing", tools=["Read", "Bash"])
        assert applied.profile is profile
        assert applied.tools == ["Read"]
        assert applied.prompt.startswith("MODE: fast")
        assert "do the thing" in applied.prompt

    def test_apply_mode_with_no_tools(self) -> None:
        profile = MODE_REGISTRY["smart"]
        applied = apply_mode(profile, prompt="hello", tools=None)
        assert applied.tools == []


class TestRegistryDefaults:
    def test_smart_deep_fast_present(self) -> None:
        assert {"smart", "deep", "fast"}.issubset(MODE_REGISTRY)

    def test_deep_has_narrower_tool_subset_than_smart(self) -> None:
        smart = MODE_REGISTRY["smart"]
        deep = MODE_REGISTRY["deep"]
        # smart accepts everything (empty allowlist), deep restricts.
        assert smart.tool_subset == ()
        assert len(deep.tool_subset) > 0

    def test_fast_has_lowest_turn_budget(self) -> None:
        budgets = {n: MODE_REGISTRY[n].max_turns for n in ("smart", "deep", "fast")}
        assert budgets["fast"] < budgets["smart"] < budgets["deep"]


class TestYamlLoading:
    def test_load_profiles_from_missing_dir_returns_empty(self, tmp_path: Path) -> None:
        assert load_profiles_from_dir(tmp_path / "does_not_exist") == {}

    def test_load_profiles_from_yaml(self, tmp_path: Path) -> None:
        (tmp_path / "custom.yaml").write_text(
            "name: custom\n"
            "system_prompt_preamble: 'CUSTOM PREAMBLE'\n"
            "tool_subset: [Read]\n"
            "temperature: 0.5\n"
            "max_turns: 25\n"
            "expected_runtime_minutes: 8\n",
            encoding="utf-8",
        )
        loaded = load_profiles_from_dir(tmp_path)
        assert "custom" in loaded
        custom = loaded["custom"]
        assert custom.system_prompt_preamble == "CUSTOM PREAMBLE"
        assert custom.tool_subset == ("Read",)
        assert custom.max_turns == 25

    def test_install_loaded_profiles_overrides_defaults(self, tmp_path: Path) -> None:
        original = MODE_REGISTRY["fast"]
        try:
            (tmp_path / "fast.yaml").write_text(
                "name: fast\n"
                "system_prompt_preamble: 'OVERRIDDEN'\n"
                "tool_subset: []\n"
                "temperature: 0.0\n"
                "max_turns: 99\n"
                "expected_runtime_minutes: 1\n",
                encoding="utf-8",
            )
            loaded = load_profiles_from_dir(tmp_path)
            install_loaded_profiles(loaded)
            assert MODE_REGISTRY["fast"].max_turns == 99
            assert MODE_REGISTRY["fast"].system_prompt_preamble == "OVERRIDDEN"
        finally:
            MODE_REGISTRY["fast"] = original

    def test_malformed_yaml_is_skipped(self, tmp_path: Path) -> None:
        (tmp_path / "bad.yaml").write_text("just a string", encoding="utf-8")
        (tmp_path / "good.yaml").write_text(
            "name: good\nsystem_prompt_preamble: ''\ntool_subset: []\n",
            encoding="utf-8",
        )
        loaded = load_profiles_from_dir(tmp_path)
        assert "bad" not in loaded
        assert "good" in loaded


class TestSpawnerPromptIntegration:
    def test_apply_mode_to_spawn_claude_yields_smart(self) -> None:
        from bernstein.core.agents.spawner_prompt import apply_mode_to_spawn

        bundle = apply_mode_to_spawn(
            model_id="claude-sonnet-4-6",
            prompt="base prompt",
            tools=["Read", "Bash", "Edit"],
        )
        assert bundle.profile.name == "smart"
        assert "base prompt" in bundle.prompt
        # smart has no tool subset → all tools pass.
        assert set(bundle.tools) == {"Read", "Bash", "Edit"}

    def test_apply_mode_to_spawn_gpt5_yields_deep_and_filters_tools(self) -> None:
        from bernstein.core.agents.spawner_prompt import apply_mode_to_spawn

        bundle = apply_mode_to_spawn(
            model_id="gpt-5.2",
            prompt="base prompt",
            tools=["Read", "Edit", "Bash", "WebFetch"],
        )
        assert bundle.profile.name == "deep"
        # deep allowlist excludes Edit and WebFetch.
        assert "Edit" not in bundle.tools
        assert "WebFetch" not in bundle.tools
        assert "Read" in bundle.tools
        assert bundle.max_turns == bundle.profile.max_turns
        assert bundle.max_turns > 40

    def test_apply_mode_to_spawn_metadata_override(self) -> None:
        from bernstein.core.agents.spawner_prompt import apply_mode_to_spawn

        task = _FakeTask(metadata={"mode": "fast"})
        bundle = apply_mode_to_spawn(
            model_id="claude-sonnet-4-6",
            prompt="base prompt",
            tools=["Read", "Bash", "Edit"],
            task=task,  # type: ignore[arg-type]
        )
        assert bundle.profile.name == "fast"
        assert "Bash" not in bundle.tools

    def test_apply_mode_to_spawn_preamble_is_prepended(self) -> None:
        from bernstein.core.agents.spawner_prompt import apply_mode_to_spawn

        bundle = apply_mode_to_spawn(
            model_id="claude-sonnet-4-6",
            prompt="<<BODY>>",
            tools=[],
        )
        assert "<<BODY>>" in bundle.prompt
        assert bundle.prompt.index("Mode:") < bundle.prompt.index("<<BODY>>")

    def test_apply_mode_to_spawn_disabled_is_passthrough(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from bernstein.core.agents import spawner_prompt as sp

        monkeypatch.setattr(sp, "MODE_PROFILES_ENABLED", False)
        bundle = sp.apply_mode_to_spawn(
            model_id="gpt-5.2",
            prompt="base prompt",
            tools=["Read", "Edit", "WebFetch"],
        )
        assert bundle.prompt == "base prompt"
        assert bundle.tools == ["Read", "Edit", "WebFetch"]


class TestOptionalSamplingFields:
    def test_new_sampling_fields_default_to_none(self) -> None:
        profile = ModeProfile(name="x", system_prompt_preamble="")
        assert profile.top_p is None
        assert profile.top_k is None
        assert profile.max_tokens is None

    def test_coerce_profile_parses_optional_sampling_fields(self) -> None:
        from bernstein.core.routing.mode_profile import _coerce_profile

        profile = _coerce_profile(
            "custom",
            {
                "system_prompt_preamble": "hi",
                "temperature": 0.4,
                "top_p": 0.85,
                "top_k": 30,
                "max_tokens": 2048,
            },
        )
        assert profile is not None
        assert profile.top_p == pytest.approx(0.85)
        assert profile.top_k == 30
        assert profile.max_tokens == 2048

    def test_coerce_profile_leaves_absent_fields_none(self) -> None:
        from bernstein.core.routing.mode_profile import _coerce_profile

        profile = _coerce_profile("custom", {"system_prompt_preamble": "hi"})
        assert profile is not None
        assert profile.top_p is None
        assert profile.top_k is None
        assert profile.max_tokens is None

    def test_apply_mode_to_spawn_bundle_carries_sampling_fields(self) -> None:
        from unittest.mock import patch

        from bernstein.core.agents.spawner_prompt import apply_mode_to_spawn
        from bernstein.core.routing.mode_profile import ModeProfile

        custom = ModeProfile(
            name="deep",
            system_prompt_preamble="",
            temperature=0.3,
            top_p=0.7,
            top_k=15,
            max_tokens=999,
        )
        with patch("bernstein.core.agents.spawner_prompt.select_mode", return_value=custom):
            bundle = apply_mode_to_spawn(model_id="gpt-5.2", prompt="p", tools=[])
        assert bundle.temperature == pytest.approx(0.3)
        assert bundle.top_p == pytest.approx(0.7)
        assert bundle.top_k == 15
        assert bundle.max_tokens == 999


class TestBundledYamlProfiles:
    def test_bundled_profiles_exist_and_load(self) -> None:
        from bernstein import _BUNDLED_TEMPLATES_DIR  # type: ignore[reportPrivateUsage]

        path = _BUNDLED_TEMPLATES_DIR / "mode_profiles"
        if not path.is_dir():
            pytest.skip("Bundled mode_profiles dir missing in this build layout")
        loaded = load_profiles_from_dir(path)
        assert {"smart", "deep", "fast"}.issubset(loaded)
