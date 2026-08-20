"""Per-adapter strategy enum tests (issue #1627).

Covers the three typed axes added to the adapter contract -- resume,
dangerous-mode, and event-channel -- plus the conformance failure when a
shipped adapter is missing its declaration, the registry-name resolution
used by ``CLIAdapter.strategy``, and back-compat with the legacy two-state
resume capability the ``bernstein resume`` env contract still depends on.
"""

from __future__ import annotations

import pytest

from bernstein.adapters._contract import (
    DEFAULT_ADAPTER_STRATEGY,
    RESUME_CAPABILITY_MATRIX,
    RESUME_FALLBACK_FRESH,
    RESUME_NATIVE,
    STRATEGY_MATRIX,
    AdapterStrategy,
    CheckpointRetryCapability,
    DangerousModeStrategy,
    EventChannel,
    OutputMode,
    ResumeStrategy,
    checkpoint_retry_capability,
    resume_capability,
    strategy_for,
    strategy_table,
    undeclared_strategies,
)
from bernstein.adapters.base import CLIAdapter
from bernstein.adapters.conformance import (
    StrategyDeclarationError,
    assert_strategies_declared,
    strategy_conformance_table,
)
from bernstein.adapters.registry import (
    get_adapter,
    iter_adapter_specs,
    registry_name_for,
)

# ---------------------------------------------------------------------------
# Enum parsing per axis
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("flag", ResumeStrategy.FLAG),
        ("flag-pair", ResumeStrategy.FLAG_PAIR),
        ("subcommand", ResumeStrategy.SUBCOMMAND),
        ("unsupported", ResumeStrategy.UNSUPPORTED),
    ],
)
def test_resume_strategy_parses_from_wire_value(raw: str, expected: ResumeStrategy) -> None:
    assert ResumeStrategy(raw) is expected
    assert str(expected) == raw


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("cli-flag", DangerousModeStrategy.CLI_FLAG),
        ("env-var", DangerousModeStrategy.ENV_VAR),
        ("always-on", DangerousModeStrategy.ALWAYS_ON),
        ("unsupported", DangerousModeStrategy.UNSUPPORTED),
    ],
)
def test_dangerous_mode_strategy_parses_from_wire_value(raw: str, expected: DangerousModeStrategy) -> None:
    assert DangerousModeStrategy(raw) is expected
    assert str(expected) == raw


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("stream-json", EventChannel.STREAM_JSON),
        ("text-signals", EventChannel.TEXT_SIGNALS),
        ("hooks", EventChannel.HOOKS),
        ("acp", EventChannel.ACP),
        ("poll-pty", EventChannel.POLL_PTY),
        ("none", EventChannel.NONE),
    ],
)
def test_event_channel_parses_from_wire_value(raw: str, expected: EventChannel) -> None:
    assert EventChannel(raw) is expected
    assert str(expected) == raw


def test_unknown_wire_value_raises() -> None:
    with pytest.raises(ValueError, match="not a valid"):
        ResumeStrategy("teleport")


# ---------------------------------------------------------------------------
# Default values
# ---------------------------------------------------------------------------


def test_default_adapter_strategy_is_conservative() -> None:
    assert DEFAULT_ADAPTER_STRATEGY.resume is ResumeStrategy.UNSUPPORTED
    assert DEFAULT_ADAPTER_STRATEGY.dangerous_mode is DangerousModeStrategy.UNSUPPORTED
    assert DEFAULT_ADAPTER_STRATEGY.event_channel is EventChannel.TEXT_SIGNALS
    assert DEFAULT_ADAPTER_STRATEGY.output_mode is OutputMode.GIT_DIFF


def test_only_the_computer_use_family_declares_artifact_output_mode() -> None:
    """The coding path stays the default; the axis has exactly one consumer.

    ``computer_use`` fronts an external agent whose unit of work is the
    signed per-action record, not a commit (#3110), so it declares
    ``artifact``. Every coding adapter keeps ``git-diff`` - growing this set
    is a deliberate act, not a default drift.
    """
    non_git_diff = {name for name, s in STRATEGY_MATRIX.items() if s.output_mode is not OutputMode.GIT_DIFF}
    assert non_git_diff == {"computer_use"}


def test_computer_use_declares_artifact_output_mode() -> None:
    assert strategy_for("computer_use").output_mode is OutputMode.ARTIFACT


def test_output_mode_is_a_closed_two_value_axis() -> None:
    assert {m.value for m in OutputMode} == {"git-diff", "artifact"}


def test_strategy_for_unknown_adapter_returns_default() -> None:
    assert strategy_for("this-adapter-does-not-exist") is DEFAULT_ADAPTER_STRATEGY


def test_adapter_strategy_to_dict_round_trips() -> None:
    strategy = AdapterStrategy(
        resume=ResumeStrategy.FLAG,
        dangerous_mode=DangerousModeStrategy.ENV_VAR,
        event_channel=EventChannel.HOOKS,
        output_mode=OutputMode.ARTIFACT,
    )
    assert strategy.to_dict() == {
        "resume": "flag",
        "dangerous_mode": "env-var",
        "event_channel": "hooks",
        "output_mode": "artifact",
    }


# ---------------------------------------------------------------------------
# Per-adapter declarations
# ---------------------------------------------------------------------------


def test_claude_declares_native_flag_resume_and_stream_json() -> None:
    strategy = strategy_for("claude")
    assert strategy.resume is ResumeStrategy.FLAG
    assert strategy.dangerous_mode is DangerousModeStrategy.CLI_FLAG
    assert strategy.event_channel is EventChannel.STREAM_JSON


def test_stream_json_adapters_declared() -> None:
    for name in ("claude", "cursor", "gemini"):
        assert strategy_for(name).event_channel is EventChannel.STREAM_JSON


def test_text_signal_default_for_plain_adapters() -> None:
    for name in ("aider", "droid", "opencode"):
        assert strategy_for(name).event_channel is EventChannel.TEXT_SIGNALS


def test_opencode_declares_native_resume_and_a_dangerous_mode_flag() -> None:
    """Both axes are backed by flags the adapter passes; the event channel is not yet."""
    strategy = strategy_for("opencode")
    assert strategy.resume is ResumeStrategy.FLAG
    assert strategy.dangerous_mode is DangerousModeStrategy.CLI_FLAG
    assert strategy.event_channel is EventChannel.TEXT_SIGNALS


def test_opencode_resume_declaration_drives_the_retry_surface() -> None:
    """The resume axis is derived into runtime decisions, not merely rendered.

    ``checkpoint_retry_capability`` is a pure function of the resume axis, so
    declaring ``flag`` promises the retry path a warm continuation. The adapter
    backs that promise via ``continuation_args``; this pins the derivation so
    the two cannot drift apart silently.
    """
    assert resume_capability("opencode") == RESUME_NATIVE
    assert checkpoint_retry_capability("opencode") is CheckpointRetryCapability.RESUME
    assert get_adapter("opencode").continuation_args("oc-1") != []


def test_continuation_optin_matches_the_resume_declaration() -> None:
    """An adapter declaring native resume must opt into the continuation path.

    ``adapter_supports_continuation`` reads ``supports_session_continuation``
    off the class; a declared-resume adapter that never opted in would leave
    the retry surface believing in a warm continuation nothing implements.
    """
    for name in ("claude", "opencode"):
        adapter = get_adapter(name)
        assert strategy_for(name).resume is not ResumeStrategy.UNSUPPORTED
        assert adapter.supports_session_continuation is True
        assert adapter.continuation_args(f"{name}-1") != []


def test_acp_channel_adapters_declared() -> None:
    for name in ("kilo", "goose"):
        assert strategy_for(name).event_channel is EventChannel.ACP


def test_every_registry_adapter_has_a_declaration() -> None:
    names = [name for name, _ in iter_adapter_specs()]
    assert undeclared_strategies(names) == []


# ---------------------------------------------------------------------------
# Conformance: missing declaration is a hard failure (AC #2)
# ---------------------------------------------------------------------------


def test_undeclared_strategies_reports_missing() -> None:
    assert undeclared_strategies(["claude", "ghost-adapter"]) == ["ghost-adapter"]


def test_assert_strategies_declared_passes_for_registry() -> None:
    # Live registry must be fully declared; raises if any adapter is missing.
    assert_strategies_declared()


def test_assert_strategies_declared_fails_on_missing() -> None:
    with pytest.raises(StrategyDeclarationError, match="ghost-adapter"):
        assert_strategies_declared(["claude", "ghost-adapter"])


# ---------------------------------------------------------------------------
# Strategy table (AC #4)
# ---------------------------------------------------------------------------


def test_strategy_table_rows_sorted_and_complete() -> None:
    rows = strategy_table(["gemini", "aider"])
    assert [r["adapter"] for r in rows] == ["aider", "gemini"]
    for row in rows:
        assert set(row) == {"adapter", "resume", "dangerous_mode", "event_channel", "output_mode"}


def test_strategy_conformance_table_covers_registry() -> None:
    rows = strategy_conformance_table()
    registry_names = {name for name, _ in iter_adapter_specs()}
    assert {r["adapter"] for r in rows} == registry_names


# ---------------------------------------------------------------------------
# CLIAdapter.strategy resolver
# ---------------------------------------------------------------------------


def test_adapter_strategy_resolves_via_registry_name() -> None:
    adapter = get_adapter("claude")
    assert registry_name_for(adapter) == "claude"
    assert adapter.strategy().resume is ResumeStrategy.FLAG


def test_inline_override_takes_precedence() -> None:
    sentinel = AdapterStrategy(
        resume=ResumeStrategy.SUBCOMMAND,
        dangerous_mode=DangerousModeStrategy.ENV_VAR,
        event_channel=EventChannel.POLL_PTY,
    )

    class _OverrideAdapter(CLIAdapter):
        strategy_override = sentinel

        def spawn(self, **_kwargs: object) -> object:  # type: ignore[override]
            raise AssertionError("not called")

        def name(self) -> str:
            return "override-stub"

    assert _OverrideAdapter().strategy() is sentinel


def test_unregistered_stub_falls_back_to_default() -> None:
    class _Stub(CLIAdapter):
        def spawn(self, **_kwargs: object) -> object:  # type: ignore[override]
            raise AssertionError("not called")

        def name(self) -> str:
            return "unregistered-stub"

    assert _Stub().strategy() is DEFAULT_ADAPTER_STRATEGY


# ---------------------------------------------------------------------------
# Back-compat: legacy two-state resume capability derives from the matrix
# ---------------------------------------------------------------------------


def test_resume_capability_native_for_flag_adapters() -> None:
    assert resume_capability("claude") == RESUME_NATIVE
    assert resume_capability("openai_agents") == RESUME_NATIVE


def test_resume_capability_fallback_for_unsupported() -> None:
    assert resume_capability("aider") == RESUME_FALLBACK_FRESH


def test_resume_capability_unknown_defaults_fallback() -> None:
    assert resume_capability("this-adapter-does-not-exist") == RESUME_FALLBACK_FRESH


def test_legacy_matrix_dict_matches_strategy_matrix() -> None:
    for name in STRATEGY_MATRIX:
        expected = (
            RESUME_NATIVE if strategy_for(name).resume is not ResumeStrategy.UNSUPPORTED else RESUME_FALLBACK_FRESH
        )
        assert RESUME_CAPABILITY_MATRIX[name] == expected
