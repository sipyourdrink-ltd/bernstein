"""A home directory that is a filesystem root must not become an empty pattern.

``collapse_home`` rewrites ``$HOME`` to ``~``. It built its pattern by
trimming the trailing separator off the home value::

    _HOME_RE = re.compile(re.escape(home.rstrip("/")))

``HOME=/`` trims to ``""``, and an empty pattern matches at every position, so
``sub`` inserts ``~`` between every character of the input. That is not a
redaction failing to fire - it is every string passing through the redactor
being rewritten character by character.

``HOME=/`` is the ordinary configuration for a container running as root. The
blast radius is not cosmetic: ``redact_text`` runs on the write path of the
work ledger *before* each entry is hashed, so the mangled text is what gets
sealed into the chain.
"""

from __future__ import annotations

import re

import pytest

from bernstein.core.security import redactor
from bernstein.core.security.redactor import collapse_home, redact_text


@pytest.fixture(autouse=True)
def _clear_home_cache() -> None:
    """The pattern cache is keyed on the home value, but not across tests."""
    redactor._HOME_CACHE = None


@pytest.mark.parametrize("home", ["/", "//", "///"])
def test_a_posix_root_home_leaves_text_untouched(home: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """On main this returns ``'~a~b~c~/~d~e~f~'``."""
    monkeypatch.setenv("HOME", home)

    assert collapse_home("abc/def") == "abc/def"


@pytest.mark.parametrize("home", ["C:\\", "C:", "z:\\"])
def test_a_windows_drive_root_home_leaves_text_untouched(home: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """``rstrip("/")`` never trimmed a backslash, so ``C:\\`` stayed ``C:\\``.

    That is not the empty-pattern catastrophe - it is a pattern that rewrites
    every absolute path on the drive to ``~`` - but it is the same mistake:
    a filesystem root is not a home prefix worth collapsing.
    """
    monkeypatch.setenv("HOME", home)

    assert collapse_home("C:\\Windows\\System32") == "C:\\Windows\\System32"


def test_the_mangling_reaches_redact_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """``collapse_home`` is the last step of the public entry point.

    This is the one that matters: ``redact_text`` is what the work ledger and
    the task mailbox call, so a corrupted result is persisted, not just shown.
    """
    monkeypatch.setenv("HOME", "/")

    cleaned, count = redact_text("the quick brown fox")

    assert cleaned == "the quick brown fox"
    assert count == 0


def test_a_real_home_still_collapses(monkeypatch: pytest.MonkeyPatch) -> None:
    """The control: the feature has to keep working.

    A fix that only ever returned ``None`` would pass every test above.
    """
    monkeypatch.setenv("HOME", "/home/alice")

    assert collapse_home("/home/alice/src/app.py") == "~/src/app.py"


def test_a_trailing_separator_on_a_real_home_is_still_trimmed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "/home/alice/")

    assert collapse_home("/home/alice/src/app.py") == "~/src/app.py"


def test_a_windows_home_collapses_and_its_trailing_backslash_is_trimmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", "C:\\Users\\alice\\")

    assert collapse_home("C:\\Users\\alice\\src\\app.py") == "~\\src\\app.py"


def test_a_unc_share_root_is_treated_as_a_real_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """It is a directory with a real prefix, unlike ``/`` and ``C:\\``.

    Pinned so the root check is not widened into "anything that looks rootish".
    """
    monkeypatch.setenv("HOME", "\\\\server\\share")

    assert collapse_home("\\\\server\\share\\notes.txt") == "~\\notes.txt"


def test_a_home_with_regex_metacharacters_is_matched_literally(monkeypatch: pytest.MonkeyPatch) -> None:
    """``re.escape`` was always there; this keeps it there."""
    monkeypatch.setenv("HOME", "/home/a+b(c)")

    assert collapse_home("/home/a+b(c)/x") == "~/x"
    assert collapse_home("/home/aXbYcZ/x") == "/home/aXbYcZ/x"


class TestThePatternCacheFollowsHome:
    """The cache is keyed on the home value it was built from.

    It used to be compiled once into a module global and kept for the life of
    the process, so a changed ``$HOME`` kept collapsing against the first one.
    ``tests/unit/orchestration/test_missions_projection.py`` had to clear that
    global by hand to make its host-independence tests meaningful at all.
    """

    def test_a_changed_home_recompiles(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", "/home/alice")
        assert collapse_home("/home/alice/x") == "~/x"

        monkeypatch.setenv("HOME", "/home/bob")
        assert collapse_home("/home/alice/x") == "/home/alice/x"
        assert collapse_home("/home/bob/x") == "~/x"

    def test_moving_to_a_root_home_recompiles_to_no_pattern(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A cached good pattern must not be what saves the root case."""
        monkeypatch.setenv("HOME", "/home/alice")
        assert collapse_home("/home/alice/x") == "~/x"

        monkeypatch.setenv("HOME", "/")
        assert collapse_home("abc") == "abc"

    def test_an_unchanged_home_is_not_recompiled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The cache still caches - this runs on every ledger string."""
        monkeypatch.setenv("HOME", "/home/alice")
        collapse_home("/home/alice/x")

        compiles = 0
        real_compile = re.compile

        def counting_compile(*args: object, **kwargs: object) -> re.Pattern[str]:
            nonlocal compiles
            compiles += 1
            return real_compile(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(re, "compile", counting_compile)
        for _ in range(10):
            collapse_home("/home/alice/x")

        assert compiles == 0
