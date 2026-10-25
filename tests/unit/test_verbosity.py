"""Tests for CLI-005: --verbose and --quiet flags."""

from __future__ import annotations

import logging

import pytest
from bernstein.cli.verbosity import (
    NORMAL,
    QUIET,
    VERBOSE,
    apply_verbosity,
    get_verbosity,
    is_quiet,
    is_verbose,
)
from click.testing import CliRunner

from bernstein.cli.main import cli


class TestVerbosityFlags:
    """Tests for --verbose and --quiet global flags."""

    def test_verbose_flag_accepted(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["--verbose", "doctor", "--help"])
        assert result.exit_code == 0

    def test_quiet_flag_accepted(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["--quiet", "doctor", "--help"])
        assert result.exit_code == 0

    def test_short_verbose_flag(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["-v", "doctor", "--help"])
        assert result.exit_code == 0

    def test_short_quiet_flag(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["-q", "doctor", "--help"])
        assert result.exit_code == 0

    def test_verbose_and_quiet_conflict(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["--verbose", "--quiet", "doctor", "--help"])
        assert result.exit_code != 0
        assert "cannot" in result.output.lower() or "error" in result.output.lower()

    def test_verbosity_constants(self) -> None:
        assert QUIET == -1
        assert NORMAL == 0
        assert VERBOSE == 1

    def test_get_verbosity_default(self) -> None:
        """get_verbosity returns NORMAL when no Click context."""
        assert get_verbosity() == NORMAL

    def test_is_verbose_default(self) -> None:
        assert is_verbose() is False

    def test_is_quiet_default(self) -> None:
        assert is_quiet() is False


class TestVerbosityDoesNotTouchTheRootLogger:
    """#6184: --quiet/--verbose reconfigured the process-wide root logger.

    ``tests/conftest.py::_restore_bernstein_logger_state`` restores the
    ``bernstein`` logger after every test, so these can mutate it directly
    without leaking into whatever test runs next.
    """

    def test_quiet_does_not_raise_the_root_logger_level(self) -> None:
        root_logger = logging.getLogger()
        before = root_logger.level

        apply_verbosity(verbose=False, quiet=True)

        assert root_logger.level == before

    def test_verbose_does_not_lower_the_root_logger_level(self) -> None:
        root_logger = logging.getLogger()
        before = root_logger.level

        apply_verbosity(verbose=True, quiet=False)

        assert root_logger.level == before

    def test_quiet_raises_the_bernstein_tree_but_not_an_unrelated_logger(self) -> None:
        """A bernstein.* module logger's effective level moves; an unrelated one does not.

        Every module in the package logs through ``logging.getLogger(__name__)``,
        so ``bernstein.core.git.worktree`` (the logger #6184's own repro uses)
        stands in for all ~900 of them.
        """
        worktree_logger = logging.getLogger("bernstein.core.git.worktree")
        other_logger = logging.getLogger("some_third_party_library")
        other_before = other_logger.getEffectiveLevel()

        apply_verbosity(verbose=False, quiet=True)

        assert worktree_logger.getEffectiveLevel() == logging.ERROR
        assert other_logger.getEffectiveLevel() == other_before

    def test_repeated_calls_do_not_stack_handlers(self) -> None:
        """A second --quiet/--verbose in the same process must not duplicate output."""
        apply_verbosity(verbose=False, quiet=True)
        apply_verbosity(verbose=False, quiet=True)
        apply_verbosity(verbose=True, quiet=False)

        assert len(logging.getLogger("bernstein").handlers) == 1

    def test_quiet_leaves_root_logger_handlers_untouched(self) -> None:
        """The old `basicConfig(force=True)` also stripped root's own handlers."""
        root_logger = logging.getLogger()
        sentinel = logging.NullHandler()
        root_logger.addHandler(sentinel)
        try:
            apply_verbosity(verbose=False, quiet=True)
            assert sentinel in root_logger.handlers
        finally:
            root_logger.removeHandler(sentinel)

    def test_a_real_quiet_cli_invocation_leaves_the_root_logger_alone(self) -> None:
        """The actual failure mode #6184 describes, through the real entry point.

        `apply_verbosity` is exercised above directly; this goes through the
        full `CliRunner.invoke` path the way #6184's own repro and every
        other test in this suite that passes `--quiet` actually do, and
        checks the property that must hold regardless of when it is
        checked: the root logger is never touched at any point, so every
        unrelated logger in the process (third-party libraries, and every
        later test in the same pytest worker) keeps its own level.
        """
        root_logger = logging.getLogger()
        other_logger = logging.getLogger("some_third_party_library")
        root_before = root_logger.level
        other_before = other_logger.getEffectiveLevel()

        CliRunner().invoke(cli, ["--quiet", "doctor", "--help"])

        assert root_logger.level == root_before
        assert other_logger.getEffectiveLevel() == other_before

    @pytest.fixture(autouse=True)
    def _restore_root_handlers(self):
        """Extra safety net local to this class: root gets a stray handler in
        one test on purpose; make sure it never survives past this class."""
        root_logger = logging.getLogger()
        saved = list(root_logger.handlers)
        yield
        root_logger.handlers = saved
