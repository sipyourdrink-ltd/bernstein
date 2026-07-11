"""Tests for the per-spawn response-style profile resolver (issue #2243).

Covers the deterministic resolution order (task metadata > role policy >
seed default > built-in ``balanced``), byte-stable addendum rendering
(AC1), the empty-addendum guarantee for ``balanced`` that keeps
no-profile spawns byte-identical to pre-change spawns (AC3), and the
typed missing-template error (AC4).
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import pytest

from bernstein.core.agents.response_style import (
    DEFAULT_RESPONSE_STYLE,
    RESPONSE_STYLES,
    STYLE_TO_MODE_PROFILE,
    ResponseStyleTemplateError,
    addendum_sha256,
    render_style_addendum,
    resolve_response_style,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


class TestVocabulary:
    def test_styles_match_conciseness_strategy_vocabulary(self) -> None:
        """The style names reuse the conciseness vocabulary from
        ``claude_model_prompts.PromptStrategy`` - no new prompt dialect."""
        from bernstein.core.agents.claude_model_prompts import (
            HAIKU_STRATEGY,
            OPUS_STRATEGY,
            SONNET_STRATEGY,
        )

        strategy_vocab = {
            OPUS_STRATEGY.conciseness,
            SONNET_STRATEGY.conciseness,
            HAIKU_STRATEGY.conciseness,
        }
        assert set(RESPONSE_STYLES) == strategy_vocab

    def test_every_style_maps_to_a_bundled_mode_profile(self) -> None:
        assert set(STYLE_TO_MODE_PROFILE) == set(RESPONSE_STYLES)
        assert set(STYLE_TO_MODE_PROFILE.values()) == {"fast", "smart", "deep"}

    def test_default_style_is_balanced(self) -> None:
        assert DEFAULT_RESPONSE_STYLE == "balanced"


# ---------------------------------------------------------------------------
# Resolution order (deterministic, documented)
# ---------------------------------------------------------------------------


class TestResolutionOrder:
    def test_builtin_default_when_nothing_is_set(self) -> None:
        resolved = resolve_response_style(task_metadata={}, role_policy={}, default_policy={})
        assert resolved.style == "balanced"
        assert resolved.source == "builtin_default"
        assert resolved.explicit is False

    def test_seed_default_beats_builtin(self) -> None:
        resolved = resolve_response_style(
            task_metadata={},
            role_policy={},
            default_policy={"response_style": "terse"},
        )
        assert resolved.style == "terse"
        assert resolved.source == "seed_default"
        assert resolved.explicit is True

    def test_role_policy_beats_seed_default(self) -> None:
        resolved = resolve_response_style(
            task_metadata={},
            role_policy={"response_style": "verbose"},
            default_policy={"response_style": "terse"},
        )
        assert resolved.style == "verbose"
        assert resolved.source == "role_policy"

    def test_task_metadata_mode_beats_role_policy(self) -> None:
        resolved = resolve_response_style(
            task_metadata={"mode": "terse"},
            role_policy={"response_style": "verbose"},
            default_policy={},
        )
        assert resolved.style == "terse"
        assert resolved.source == "task_metadata"

    @pytest.mark.parametrize(
        ("mode_name", "expected_style"),
        [("fast", "terse"), ("smart", "balanced"), ("deep", "verbose")],
    )
    def test_task_metadata_accepts_mode_profile_names(self, mode_name: str, expected_style: str) -> None:
        """``Task.metadata['mode']`` already carries fast/smart/deep for the
        mode-profile layer; those names map deterministically onto styles."""
        resolved = resolve_response_style(
            task_metadata={"mode": mode_name},
            role_policy={},
            default_policy={},
        )
        assert resolved.style == expected_style
        assert resolved.source == "task_metadata"

    def test_unknown_metadata_mode_falls_through(self) -> None:
        resolved = resolve_response_style(
            task_metadata={"mode": "warp-speed"},
            role_policy={"response_style": "terse"},
            default_policy={},
        )
        assert resolved.style == "terse"
        assert resolved.source == "role_policy"

    def test_unknown_role_policy_style_falls_through(self) -> None:
        resolved = resolve_response_style(
            task_metadata={},
            role_policy={"response_style": "shouty"},
            default_policy={},
        )
        assert resolved.style == "balanced"
        assert resolved.source == "builtin_default"

    def test_same_inputs_always_resolve_identically(self) -> None:
        args = {
            "task_metadata": {"mode": "verbose"},
            "role_policy": {"response_style": "terse"},
            "default_policy": {"response_style": "balanced"},
        }
        first = resolve_response_style(**args)
        second = resolve_response_style(**args)
        assert first == second


# ---------------------------------------------------------------------------
# Rendering (AC1: byte-identical snapshot; AC3: balanced renders empty)
# ---------------------------------------------------------------------------

_EXPECTED_TERSE_ADDENDUM = (
    "## Response style: terse\n"
    "\n"
    "## Mode: fast (terse, minimal tool use)\n"
    "You operate in fast mode. Produce the shortest correct answer.\n"
    "Avoid exploratory tool calls; prefer a single well-targeted action.\n"
    "\n"
    "### Style carve-outs (verbatim zones)\n"
    "The response style applies to prose only. Never compress, truncate, or\n"
    "restyle code blocks, commit messages, error text, or completion API JSON\n"
    "payloads; reproduce them in full."
)

_EXPECTED_VERBOSE_ADDENDUM = (
    "## Response style: verbose\n"
    "\n"
    "## Mode: deep (long autonomous research)\n"
    "You operate in deep autonomous mode. Plan thoroughly before acting,\n"
    "minimise external chatter, and only surface findings when you have a\n"
    "complete answer. Use tools sparingly and prefer reasoning.\n"
    "\n"
    "### Style carve-outs (verbatim zones)\n"
    "The response style applies to prose only. Never compress, truncate, or\n"
    "restyle code blocks, commit messages, error text, or completion API JSON\n"
    "payloads; reproduce them in full."
)


class TestRendering:
    def test_terse_addendum_snapshot(self) -> None:
        """AC1: given identical config, the rendered addendum is
        byte-identical (frozen snapshot of the bundled template render)."""
        assert render_style_addendum("terse") == _EXPECTED_TERSE_ADDENDUM

    def test_verbose_addendum_snapshot(self) -> None:
        assert render_style_addendum("verbose") == _EXPECTED_VERBOSE_ADDENDUM

    def test_balanced_renders_empty(self) -> None:
        """AC3: balanced is the neutral default; it renders no addendum so
        spawns without a profile stay byte-identical to pre-change spawns."""
        assert render_style_addendum("balanced") == ""

    def test_render_is_stable_across_calls(self) -> None:
        assert render_style_addendum("terse") == render_style_addendum("terse")

    def test_workdir_template_overrides_bundled(self, tmp_path: Path) -> None:
        profiles = tmp_path / "templates" / "mode_profiles"
        profiles.mkdir(parents=True)
        (profiles / "fast.yaml").write_text(
            "name: fast\nsystem_prompt_preamble: |\n  Custom terse preamble.\n",
            encoding="utf-8",
        )
        rendered = render_style_addendum("terse", workdir=tmp_path)
        assert "Custom terse preamble." in rendered
        assert rendered.startswith("## Response style: terse\n")

    def test_unknown_style_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="unknown response style"):
            render_style_addendum("shouty")

    def test_missing_template_raises_typed_error(self, tmp_path: Path) -> None:
        """AC4: a workdir profile dir that lacks the mapped template file
        fails with the typed error instead of silently falling back."""
        profiles = tmp_path / "templates" / "mode_profiles"
        profiles.mkdir(parents=True)
        (profiles / "deep.yaml").write_text(
            "name: deep\nsystem_prompt_preamble: |\n  Deep preamble.\n",
            encoding="utf-8",
        )
        with pytest.raises(ResponseStyleTemplateError, match="fast.yaml"):
            render_style_addendum("terse", workdir=tmp_path)

    def test_malformed_template_raises_typed_error(self, tmp_path: Path) -> None:
        profiles = tmp_path / "templates" / "mode_profiles"
        profiles.mkdir(parents=True)
        (profiles / "fast.yaml").write_text("- not\n- a\n- mapping\n", encoding="utf-8")
        with pytest.raises(ResponseStyleTemplateError):
            render_style_addendum("terse", workdir=tmp_path)


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


class TestHashing:
    def test_sha256_of_rendered_addendum(self) -> None:
        rendered = render_style_addendum("terse")
        expected = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
        assert addendum_sha256(rendered) == expected

    def test_empty_addendum_hash_is_sha256_of_empty_bytes(self) -> None:
        assert addendum_sha256("") == hashlib.sha256(b"").hexdigest()
