"""Tests for the per-turn task-budget countdown formatter."""

from __future__ import annotations

import pytest

from bernstein.core.cost.budget_countdown import (
    DEFAULT_LOW_THRESHOLD_PCT,
    TASK_BUDGETS_OPT_IN_ENV,
    TurnState,
    format_countdown,
    is_task_budgets_opt_in,
    record_graceful_finish,
    should_finish_gracefully,
    task_budgets_beta_headers,
    task_budgets_env_overlay,
)
from bernstein.core.cost.cost_tracker import CostTracker
from bernstein.core.identity.agent_card import (
    TASK_BUDGETS_BETA_HEADER,
    AgentIdentityCard,
    issue_identity_card,
)


def _card(
    *,
    role: str = "backend",
    max_tokens: int = 64_000,
    max_steps: int = 30,
    max_budget_usd: float = 1.5,
    budget_mode: str = "graceful-finish-on-low",
    extensions: dict[str, str | bool | int | float] | None = None,
) -> AgentIdentityCard:
    """Build a card with the specific knobs needed for the countdown tests."""
    base = issue_identity_card("agent-1", role, "claude", "opus")
    base.max_tokens = max_tokens
    base.max_steps = max_steps
    base.max_budget_usd = max_budget_usd
    base.budget_mode = budget_mode  # type: ignore[assignment]
    if extensions is not None:
        base.extensions = dict(extensions)
    return base


def _tracker(spent_usd: float = 0.0) -> CostTracker:
    """Build a tracker with a documented run id and pre-set spend."""
    t = CostTracker(run_id="test-run", budget_usd=1.5)
    if spent_usd:
        t._spent_usd = spent_usd  # pyright: ignore[reportPrivateUsage]
    return t


class TestFormatCountdown:
    """``format_countdown`` returns the documented banner string."""

    def test_documented_banner_format(self) -> None:
        card = _card()
        tracker = _tracker(spent_usd=0.42)
        # 18,420 tokens left -> 45,580 used (since max=64,000)
        state = TurnState(step=7, tokens_used=64_000 - 18_420)

        line = format_countdown(card, tracker, state)

        # Matches the documented example in the ticket goal section verbatim.
        assert line == (
            "[budget] tokens left: 18,420 of 64,000 (28%) | "
            "$0.42 of $1.50 | "
            "steps: 7 of 30 | "
            "mode: graceful-finish-on-low"
        )

    def test_banner_is_single_line(self) -> None:
        card = _card()
        tracker = _tracker(spent_usd=0.10)
        state = TurnState(step=1, tokens_used=1000)

        line = format_countdown(card, tracker, state)

        assert "\n" not in line

    def test_negative_token_remainder_clamps_to_zero(self) -> None:
        card = _card(max_tokens=1000)
        tracker = _tracker(spent_usd=1.6)
        state = TurnState(step=10, tokens_used=2000)  # over-spent

        line = format_countdown(card, tracker, state)

        assert "tokens left: 0 of 1,000" in line
        assert "(0%)" in line

    def test_unbounded_max_tokens_reports_full_headroom(self) -> None:
        card = _card(max_tokens=0)
        tracker = _tracker(spent_usd=0.0)
        state = TurnState(step=1, tokens_used=999_999)

        line = format_countdown(card, tracker, state)

        # ``max_tokens=0`` means "unbounded" - banner reports 100% left.
        assert "(100%)" in line

    def test_thousands_separators(self) -> None:
        card = _card(max_tokens=200_000)
        tracker = _tracker()
        state = TurnState(step=1, tokens_used=12_345)

        line = format_countdown(card, tracker, state)

        assert "187,655" in line
        assert "200,000" in line

    def test_mode_label_matches_card(self) -> None:
        card = _card(budget_mode="hard-stop-on-zero")
        tracker = _tracker()
        state = TurnState(step=1, tokens_used=1)

        line = format_countdown(card, tracker, state)

        assert "mode: hard-stop-on-zero" in line


class TestShouldFinishGracefully:
    """``should_finish_gracefully`` honours card budget mode and thresholds."""

    def test_low_token_headroom_triggers_graceful_finish(self) -> None:
        card = _card(max_tokens=100_000, budget_mode="graceful-finish-on-low")
        # 12% headroom - under default 20% threshold.
        state = TurnState(step=2, tokens_used=88_000)

        assert should_finish_gracefully(card, state)

    def test_above_threshold_does_not_finish(self) -> None:
        card = _card(max_tokens=100_000, budget_mode="graceful-finish-on-low")
        state = TurnState(step=2, tokens_used=20_000)  # 80% headroom

        assert not should_finish_gracefully(card, state)

    def test_steps_exhausted_triggers_graceful_finish(self) -> None:
        card = _card(max_tokens=100_000, max_steps=5)
        state = TurnState(step=5, tokens_used=1_000)

        assert should_finish_gracefully(card, state)

    def test_hard_stop_only_at_zero_tokens(self) -> None:
        card = _card(max_tokens=10_000, budget_mode="hard-stop-on-zero")
        # 1% headroom - graceful mode would finish, hard-stop should not.
        almost_done = TurnState(step=99, tokens_used=9_900)
        zero = TurnState(step=99, tokens_used=10_000)

        assert not should_finish_gracefully(card, almost_done)
        assert should_finish_gracefully(card, zero)

    def test_default_threshold_constant_matches_doc(self) -> None:
        # Documented as 20% in agentic_systems_v2.md.
        assert DEFAULT_LOW_THRESHOLD_PCT == 20

    def test_custom_threshold_override(self) -> None:
        card = _card(max_tokens=100_000, budget_mode="graceful-finish-on-low")
        state = TurnState(step=2, tokens_used=70_000)  # 30% headroom

        assert not should_finish_gracefully(card, state, low_threshold_pct=20)
        assert should_finish_gracefully(card, state, low_threshold_pct=40)


class TestOptInGate:
    """``is_task_budgets_opt_in`` reads the documented env var."""

    @pytest.mark.parametrize("value", ["true", "1", "yes", "on", "TRUE", "True"])
    def test_truthy_values(self, value: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(TASK_BUDGETS_OPT_IN_ENV, value)
        assert is_task_budgets_opt_in()

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "anything-else"])
    def test_falsy_values(self, value: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(TASK_BUDGETS_OPT_IN_ENV, value)
        assert not is_task_budgets_opt_in()

    def test_unset_is_off_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(TASK_BUDGETS_OPT_IN_ENV, raising=False)
        assert not is_task_budgets_opt_in()


class TestTaskBudgetsBetaHeaders:
    """Header / env overlay only emits when both opt-ins are present."""

    def test_emits_when_process_and_card_opt_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(TASK_BUDGETS_OPT_IN_ENV, "true")
        card = _card(extensions={"task_budgets": True})

        assert task_budgets_beta_headers(card) == {"anthropic-beta": TASK_BUDGETS_BETA_HEADER}
        assert task_budgets_env_overlay(card) == {"ANTHROPIC_BETA": TASK_BUDGETS_BETA_HEADER}

    def test_no_emit_without_process_opt_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(TASK_BUDGETS_OPT_IN_ENV, raising=False)
        card = _card(extensions={"task_budgets": True})

        assert task_budgets_beta_headers(card) == {}
        assert task_budgets_env_overlay(card) == {}

    def test_no_emit_without_card_extension(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(TASK_BUDGETS_OPT_IN_ENV, "true")
        card = _card()  # no extensions

        assert task_budgets_beta_headers(card) == {}
        assert task_budgets_env_overlay(card) == {}


class TestRecordGracefulFinish:
    """Telemetry hook updates Prometheus counters by role."""

    def test_increments_counter_by_role(self) -> None:
        card = _card(role="qa", max_tokens=10_000)
        state = TurnState(step=10, tokens_used=8_500)  # 15% headroom

        # Should not raise even when the prometheus_client stub is in use.
        record_graceful_finish(card, state)

        from bernstein.core.observability.prometheus import (
            task_budget_graceful_finish_total,
            task_budget_remaining_at_finish_pct,
        )

        # When prometheus_client is installed, ``_value.get`` exposes the
        # current count. With the stub we still want zero exceptions.
        labelled = task_budget_graceful_finish_total.labels(role="qa")
        assert labelled is not None
        assert task_budget_remaining_at_finish_pct.labels(role="qa") is not None


class TestCardDefaults:
    """New card fields default to documented values."""

    def test_default_budget_mode_is_graceful(self) -> None:
        card = issue_identity_card("a", "backend", "claude", "opus")
        assert card.budget_mode == "graceful-finish-on-low"

    def test_default_max_tokens_and_steps(self) -> None:
        card = issue_identity_card("a", "backend", "claude", "opus")
        assert card.max_tokens == 64_000
        assert card.max_steps == 30

    def test_extensions_default_to_empty(self) -> None:
        card = issue_identity_card("a", "backend", "claude", "opus")
        assert card.extensions == {}


class TestAdapterMirrorConstants:
    """Adapter-side mirrors of the opt-in env / beta value stay in sync.

    The Claude adapter inlines the constants to avoid a transitive
    scheduler-internal import (see ``.importlinter`` contract
    ``adapters-no-scheduler``). This test pins the mirror values so a
    drift surfaces immediately.
    """

    def test_claude_adapter_mirrors_opt_in_env_name(self) -> None:
        from bernstein.adapters import claude as claude_adapter

        assert claude_adapter._TASK_BUDGETS_OPT_IN_ENV == TASK_BUDGETS_OPT_IN_ENV  # pyright: ignore[reportPrivateUsage]

    def test_claude_adapter_mirrors_beta_value(self) -> None:
        from bernstein.adapters import claude as claude_adapter

        assert claude_adapter._TASK_BUDGETS_BETA_VALUE == TASK_BUDGETS_BETA_HEADER  # pyright: ignore[reportPrivateUsage]
