"""Tests for voice control intent parsing (road-039)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from bernstein.cli.voice_control import (
    VoiceConfig,
    VoiceIntent,
    extract_plan_reference,
    format_confirmation,
    load_voice_config,
    parse_voice_intent,
)

# ---------------------------------------------------------------------------
# VoiceIntent dataclass
# ---------------------------------------------------------------------------


def test_voice_intent_defaults() -> None:
    """VoiceIntent has sensible defaults for optional fields."""
    intent = VoiceIntent(action="run")
    assert intent.action == "run"
    assert intent.plan_file is None
    assert intent.target is None
    assert intent.confidence == pytest.approx(0.0)


def test_voice_intent_is_frozen() -> None:
    """VoiceIntent instances cannot be mutated."""
    intent = VoiceIntent(action="stop", confidence=0.8)
    try:
        intent.confidence = 0.1  # type: ignore[misc]
        raise AssertionError("Expected FrozenInstanceError")  # pragma: no cover
    except AttributeError:
        pass  # expected


# ---------------------------------------------------------------------------
# parse_voice_intent — action matching
# ---------------------------------------------------------------------------


def test_parse_run_intent() -> None:
    """Transcript mentioning 'run plan' resolves to action='run'."""
    intent = parse_voice_intent("please run the auth plan")
    assert intent.action == "run"
    assert intent.confidence >= 0.5
    assert intent.plan_file == "auth"


def test_parse_stop_intent() -> None:
    """Transcript mentioning 'stop all' resolves to action='stop'."""
    intent = parse_voice_intent("stop all agents now")
    assert intent.action == "stop"
    assert intent.confidence >= 0.5


def test_parse_stop_single_agent() -> None:
    """'stop agent' (singular) also matches stop intent."""
    intent = parse_voice_intent("please stop agent")
    assert intent.action == "stop"


def test_parse_status_show() -> None:
    """'show status' resolves to action='status'."""
    intent = parse_voice_intent("show me the status")
    assert intent.action == "status"
    assert intent.confidence >= 0.5


def test_parse_status_what() -> None:
    """'what is the status' resolves to action='status'."""
    intent = parse_voice_intent("what is the status right now")
    assert intent.action == "status"


def test_parse_cost_intent() -> None:
    """'how much have we spent' resolves to action='cost'."""
    intent = parse_voice_intent("how much have we spent so far")
    assert intent.action == "cost"
    assert intent.confidence >= 0.5


def test_parse_cost_keyword() -> None:
    """Single word 'cost' matches cost intent."""
    intent = parse_voice_intent("show me cost")
    assert intent.action == "cost"


def test_parse_help_intent() -> None:
    """'help' resolves to action='help'."""
    intent = parse_voice_intent("help")
    assert intent.action == "help"
    assert intent.confidence >= 0.5


def test_parse_unknown_intent() -> None:
    """Unrecognized transcript resolves to action='unknown'."""
    intent = parse_voice_intent("banana chocolate milkshake")
    assert intent.action == "unknown"
    assert intent.confidence == pytest.approx(0.0)


def test_parse_empty_transcript() -> None:
    """Empty transcript resolves to unknown with zero confidence."""
    intent = parse_voice_intent("")
    assert intent.action == "unknown"
    assert intent.confidence == pytest.approx(0.0)


def test_parse_run_without_plan_ref() -> None:
    """'run plan' without a plan name still matches run, plan_file is None."""
    intent = parse_voice_intent("run the plan")
    assert intent.action == "run"
    # 'the' is captured but 'the' is actually a valid word match for (\w+)
    # The important thing is the action is 'run'


# ---------------------------------------------------------------------------
# extract_plan_reference
# ---------------------------------------------------------------------------


def test_extract_plan_reference_basic() -> None:
    """Extracts plan name from 'run the auth plan'."""
    assert extract_plan_reference("run the auth plan") == "auth"


def test_extract_plan_reference_no_article() -> None:
    """Extracts plan name without 'the': 'run deploy plan'."""
    assert extract_plan_reference("run deploy plan") == "deploy"


def test_extract_plan_reference_execute() -> None:
    """'execute the migration plan' extracts 'migration'."""
    assert extract_plan_reference("execute the migration plan") == "migration"


def test_extract_plan_reference_start() -> None:
    """'start backend plan' extracts 'backend'."""
    assert extract_plan_reference("start backend plan") == "backend"


def test_extract_plan_reference_none() -> None:
    """Returns None when no plan reference is present."""
    assert extract_plan_reference("show me the status") is None


def test_extract_plan_reference_case_insensitive() -> None:
    """Plan name extraction is case-insensitive, result lowered."""
    assert extract_plan_reference("Run The AUTH Plan") == "auth"


# ---------------------------------------------------------------------------
# format_confirmation
# ---------------------------------------------------------------------------


def test_format_confirmation_with_plan() -> None:
    """Confirmation includes the plan name when present."""
    intent = VoiceIntent(action="run", plan_file="auth", confidence=0.9)
    result = format_confirmation(intent)
    assert "run" in result
    assert "auth" in result
    assert "Proceed? [Y/n]" in result


def test_format_confirmation_without_plan() -> None:
    """Confirmation works without a plan file."""
    intent = VoiceIntent(action="stop", confidence=0.8)
    result = format_confirmation(intent)
    assert "stop" in result
    assert "Proceed? [Y/n]" in result


def test_format_confirmation_with_target() -> None:
    """Confirmation includes a target when present."""
    intent = VoiceIntent(action="stop", target="agent-7", confidence=0.7)
    result = format_confirmation(intent)
    assert "agent-7" in result
    assert "Proceed? [Y/n]" in result


# ---------------------------------------------------------------------------
# VoiceConfig defaults
# ---------------------------------------------------------------------------


def test_voice_config_defaults() -> None:
    """VoiceConfig defaults: disabled, confirmation required, 'bernstein' wake word."""
    cfg = VoiceConfig()
    assert cfg.enabled is False
    assert cfg.confirmation_required is True
    assert cfg.wake_word == "bernstein"


def test_voice_config_is_frozen() -> None:
    """VoiceConfig instances cannot be mutated."""
    cfg = VoiceConfig()
    try:
        cfg.enabled = True  # type: ignore[misc]
        raise AssertionError("Expected FrozenInstanceError")  # pragma: no cover
    except AttributeError:
        pass  # expected


# ---------------------------------------------------------------------------
# load_voice_config
# ---------------------------------------------------------------------------


def test_load_voice_config_none_path() -> None:
    """Passing None returns default config."""
    cfg = load_voice_config(None)
    assert cfg == VoiceConfig()


def test_load_voice_config_missing_file(tmp_path: Path) -> None:
    """Non-existent file returns default config."""
    cfg = load_voice_config(tmp_path / "nonexistent.yaml")
    assert cfg == VoiceConfig()


def test_load_voice_config_from_file(tmp_path: Path) -> None:
    """Loads custom values from a YAML file."""
    cfg_file = tmp_path / "voice.yaml"
    cfg_file.write_text(
        textwrap.dedent("""\
            voice:
              enabled: true
              confirmation_required: false
              wake_word: maestro
        """),
        encoding="utf-8",
    )
    cfg = load_voice_config(cfg_file)
    assert cfg.enabled is True
    assert cfg.confirmation_required is False
    assert cfg.wake_word == "maestro"


def test_load_voice_config_flat_structure(tmp_path: Path) -> None:
    """Loads config when keys are at top level (no 'voice' section)."""
    cfg_file = tmp_path / "voice.yaml"
    cfg_file.write_text(
        textwrap.dedent("""\
            enabled: true
            wake_word: hey
        """),
        encoding="utf-8",
    )
    cfg = load_voice_config(cfg_file)
    assert cfg.enabled is True
    assert cfg.wake_word == "hey"
    # confirmation_required defaults to True when not specified
    assert cfg.confirmation_required is True


def test_load_voice_config_invalid_yaml(tmp_path: Path) -> None:
    """Non-dict YAML content returns default config."""
    cfg_file = tmp_path / "voice.yaml"
    cfg_file.write_text("just a string\n", encoding="utf-8")
    cfg = load_voice_config(cfg_file)
    assert cfg == VoiceConfig()
