"""Tests for claude_exit_codes — exit code to AbortReason/TransitionReason mapping."""

from __future__ import annotations

from bernstein.core.models import AbortReason, TransitionReason

from bernstein.adapters.claude_exit_codes import (
    ExitInterpretation,
    interpret_exit_code,
    interpret_result_subtype,
)

# ---------------------------------------------------------------------------
# interpret_exit_code
# ---------------------------------------------------------------------------


class TestInterpretExitCode:
    def test_exit_0_is_success(self) -> None:
        result = interpret_exit_code(0)
        assert result.exit_code == 0
        assert result.transition_reason == TransitionReason.COMPLETED
        assert result.abort_reason is None
        assert result.should_retry is False
        assert "success" in result.human_readable.lower()

    def test_exit_1_is_general_error(self) -> None:
        result = interpret_exit_code(1)
        assert result.transition_reason == TransitionReason.ABORTED
        assert result.abort_reason == AbortReason.PROVIDER_ERROR
        assert result.should_retry is True

    def test_exit_2_is_user_interrupt(self) -> None:
        result = interpret_exit_code(2)
        assert result.transition_reason == TransitionReason.ABORTED
        assert result.abort_reason == AbortReason.USER_INTERRUPT
        assert result.should_retry is False

    def test_exit_3_is_context_overflow(self) -> None:
        result = interpret_exit_code(3)
        assert result.transition_reason == TransitionReason.PROMPT_TOO_LONG
        assert result.abort_reason == AbortReason.COMPACT_FAILURE
        assert result.should_retry is True

    def test_exit_4_is_permission_denied(self) -> None:
        result = interpret_exit_code(4)
        assert result.transition_reason == TransitionReason.PERMISSION_DENIED
        assert result.abort_reason == AbortReason.PERMISSION_DENIED
        assert result.should_retry is False

    def test_exit_130_is_sigint(self) -> None:
        result = interpret_exit_code(130)
        assert result.abort_reason == AbortReason.USER_INTERRUPT
        assert result.should_retry is False

    def test_exit_137_is_sigkill(self) -> None:
        result = interpret_exit_code(137)
        assert result.abort_reason == AbortReason.OOM
        assert result.should_retry is True

    def test_exit_143_is_sigterm(self) -> None:
        result = interpret_exit_code(143)
        assert result.abort_reason == AbortReason.SHUTDOWN_SIGNAL
        assert result.should_retry is True

    def test_unknown_signal_exit_code(self) -> None:
        # 128 + 6 = SIGABRT
        result = interpret_exit_code(134)
        assert result.transition_reason == TransitionReason.ABORTED
        assert result.abort_reason == AbortReason.UNKNOWN
        assert result.should_retry is True
        assert "signal 6" in result.human_readable

    def test_unknown_nonzero_exit_code(self) -> None:
        result = interpret_exit_code(42)
        assert result.transition_reason == TransitionReason.ABORTED
        assert result.abort_reason == AbortReason.UNKNOWN
        assert result.should_retry is True
        assert "42" in result.human_readable

    def test_all_mapped_codes_return_valid_interpretation(self) -> None:
        for code in (0, 1, 2, 3, 4, 130, 137, 143):
            result = interpret_exit_code(code)
            assert isinstance(result, ExitInterpretation)
            assert isinstance(result.transition_reason, TransitionReason)

    def test_exit_interpretation_fields_are_populated(self) -> None:
        result = interpret_exit_code(1)
        assert result.exit_code == 1
        assert result.human_readable != ""


# ---------------------------------------------------------------------------
# interpret_result_subtype
# ---------------------------------------------------------------------------


class TestInterpretResultSubtype:
    def test_success_subtype(self) -> None:
        result = interpret_result_subtype("success")
        assert result.transition_reason == TransitionReason.COMPLETED
        assert result.abort_reason is None
        assert result.should_retry is False

    def test_error_max_turns(self) -> None:
        result = interpret_result_subtype("error_max_turns")
        assert result.transition_reason == TransitionReason.MAX_TURNS
        assert result.should_retry is True

    def test_error_model(self) -> None:
        result = interpret_result_subtype("error_model")
        assert result.transition_reason == TransitionReason.ABORTED
        assert result.abort_reason == AbortReason.PROVIDER_ERROR
        assert result.should_retry is True

    def test_error_context_window(self) -> None:
        result = interpret_result_subtype("error_context_window")
        assert result.transition_reason == TransitionReason.PROMPT_TOO_LONG
        assert result.abort_reason == AbortReason.COMPACT_FAILURE
        assert result.should_retry is True

    def test_error_permission(self) -> None:
        result = interpret_result_subtype("error_permission")
        assert result.transition_reason == TransitionReason.PERMISSION_DENIED
        assert result.abort_reason == AbortReason.PERMISSION_DENIED
        assert result.should_retry is False

    def test_unknown_subtype(self) -> None:
        result = interpret_result_subtype("something_unexpected")
        assert result.transition_reason == TransitionReason.ABORTED
        assert result.abort_reason == AbortReason.UNKNOWN
        assert result.should_retry is True

    def test_exit_code_is_minus_one_for_subtypes(self) -> None:
        """Result subtypes don't come from exit codes, so exit_code = -1."""
        result = interpret_result_subtype("success")
        assert result.exit_code == -1
