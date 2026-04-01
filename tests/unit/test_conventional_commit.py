"""Tests for conventional commit message validation and generation."""

from __future__ import annotations

import pytest


class TestIsConventionalCommitMessage:
    """Tests for ``is_conventional_commit_message``."""

    @staticmethod
    def _check(message: str) -> bool:
        from bernstein.core.git_basic import is_conventional_commit_message

        return is_conventional_commit_message(message)

    # -- valid messages -------------------------------------------------------

    @pytest.mark.parametrize(
        "message",
        [
            "feat: add new widget",
            "fix: resolve crash on startup",
            "chore: clean up old files",
            "docs: update README",
            "test: add integration test for auth",
            "refactor: simplify router logic",
            "feat(core): implement batch API",
            "fix(cli): handle missing config",
            "chore(deps): upgrade pydantic",
            "docs(api): document webhook endpoint",
            "test(unit): cover edge cases",
            "refactor(models): split Task dataclass",
            "feat(core/router): add fallback route",
            "feat: add feature\n\nSome body text describing the change.",
            "fix: correct typo\n\nRefs: #123",
        ],
        ids=[
            "feat",
            "fix",
            "chore",
            "docs",
            "test",
            "refactor",
            "feat-with-scope",
            "fix-with-scope",
            "chore-with-scope",
            "docs-with-scope",
            "test-with-scope",
            "refactor-with-scope",
            "nested-scope",
            "with-body",
            "with-footer",
        ],
    )
    def test_valid_messages(self, message: str) -> None:
        assert self._check(message) is True

    # -- invalid messages -----------------------------------------------------

    @pytest.mark.parametrize(
        "message",
        [
            "",
            "   ",
            "just a plain message",
            "Feat: capitalized prefix",
            "feature: wrong prefix",
            "feat:",
            "feat:missing space",
            "merge: not a valid type",
            "wip: work in progress",
        ],
        ids=[
            "empty",
            "whitespace-only",
            "no-prefix",
            "capitalized-prefix",
            "wrong-prefix",
            "no-description",
            "no-space-after-colon",
            "merge-type",
            "wip-type",
        ],
    )
    def test_invalid_messages(self, message: str) -> None:
        assert self._check(message) is False

    def test_multiline_skips_blank_leading_lines(self) -> None:
        """Leading blank lines should be skipped; first non-blank line is the subject."""
        msg = "\n\n  feat: delayed subject\n\nBody here."
        assert self._check(msg) is True

    def test_multiline_invalid_subject(self) -> None:
        msg = "\n\n  not conventional\n\nfeat: buried in body"
        assert self._check(msg) is False
