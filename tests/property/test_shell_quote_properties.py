"""Property tests for shell quoting and shlex round-trips.

Bernstein assembles subprocess commands from operator-supplied strings
(``benchmark_command``, ``coverage_command``, ``auto_format_*``, etc.)
and pipes them through ``shlex.split`` / ``shlex.quote`` /
``shlex.join``. A malformed quoting helper here translates directly to
command injection on the runner.

Properties:

* **``shlex.split(shlex.join(parts))`` round-trips for arbitrary
  argv** - the canonical invariant for safe command construction.
  Catches regressions where a refactor introduces ``" ".join(args)``
  on a path that previously called ``shlex.join``.

* **``shell_quote`` produces single-argv tokens** - feeding
  ``shell_quote(s)`` back through ``shlex.split`` always yields
  ``[s]`` (modulo whitespace-only edge cases). This is the contract
  the production CLI relies on for ``ssh_backend.write_file`` and
  similar shell-spliced calls.

* **Injection metacharacters are not exposed** - strings containing
  ``;``, ``$(...)``, backticks, ``|``, and ``&&`` survive a
  ``shell_quote`` → ``shlex.split`` round-trip as a single argv
  token. This is the security guarantee: if it ever splits into
  multiple tokens, an attacker controls subsequent shell tokens.

* **Empty / whitespace-only strings round-trip safely** - ``""`` →
  ``"''"`` → ``[""]``. Catches regressions that drop the explicit
  empty quoting branch and accidentally collapse to ``[]``.

Each property uses the smoke profile (50 examples) - these are
microsecond operations and there's no IO to slow them down.
"""

from __future__ import annotations

import shlex
from unittest.mock import patch

from hypothesis import assume, given
from hypothesis import strategies as st

from bernstein.core.config.platform_compat import shell_quote
from bernstein.core.workflows.workflow_runner import shell_join

# ``shell_quote`` is platform-aware: on POSIX it delegates to ``shlex.quote``
# and the ``shlex.split`` round-trip below is the security contract; on
# Windows it emits ``cmd.exe`` double-quote form that intentionally does NOT
# round-trip through POSIX ``shlex``. These properties pin the POSIX branch,
# so they force ``IS_WINDOWS=False`` and therefore run identically on every
# host (including the Windows lane) instead of skipping there. The Windows
# branch has its own dedicated contract test below.
_FORCE_POSIX = patch("bernstein.core.config.platform_compat.IS_WINDOWS", False)

# Characters that survive shlex round-trip without ambiguity. We exclude
# code points that shlex's POSIX mode treats specially as line
# terminators inside a quoted token (the only one that matters in
# practice is the literal newline; shlex.split with posix=True will
# happily split on ``\n`` inside ``$'...'`` constructs we don't use).
_TOKEN_ALPHABET = st.characters(
    blacklist_categories=("Cs", "Cc"),
    min_codepoint=0x20,
    max_codepoint=0x7E,
) | st.sampled_from([" ", "\t"])

# Characters known to be hostile to shells. The point of the property
# is to confirm shlex still produces a single token when the input
# embeds them verbatim.
_INJECTION_CHARS = st.sampled_from([";", "&", "|", "$", "`", "(", ")", "<", ">", "\\", '"', "'"])


def _argv_strategy() -> st.SearchStrategy[list[str]]:
    return st.lists(
        st.text(_TOKEN_ALPHABET, min_size=1, max_size=16),
        min_size=1,
        max_size=6,
    )


@given(argv=_argv_strategy())
def test_split_join_round_trip(argv: list[str]) -> None:
    """``shlex.split(shell_join(argv)) == argv`` for arbitrary argv.

    The single critical invariant for safe command construction. A
    failure here means any caller that builds a command via
    ``shell_join`` and later re-parses with ``shlex.split`` will get
    the wrong argument boundary - the root cause of every shell
    injection bug we've ever closed.
    """
    joined = shell_join(argv)
    assert shlex.split(joined) == argv


@_FORCE_POSIX
@given(token=st.text(_TOKEN_ALPHABET, min_size=0, max_size=32))
def test_shell_quote_produces_single_token(token: str) -> None:
    """``shlex.split(shell_quote(s))`` always yields ``[s]`` on POSIX.

    Empty strings are explicitly part of the contract: ``shell_quote("")``
    must produce ``"''"`` so the empty argv is preserved. Without this,
    callers building ``f"foo {shell_quote(x)} bar"`` would silently
    swallow blank arguments.
    """
    quoted = shell_quote(token)
    parts = shlex.split(quoted)
    assert parts == [token]


@_FORCE_POSIX
@given(
    prefix=st.text(_TOKEN_ALPHABET, min_size=0, max_size=8),
    injection=_INJECTION_CHARS,
    suffix=st.text(_TOKEN_ALPHABET, min_size=0, max_size=8),
)
def test_injection_chars_do_not_escape_quoting(
    prefix: str,
    injection: str,
    suffix: str,
) -> None:
    """Hostile shell metacharacters survive quoting as part of one token.

    If ``shell_quote("foo; rm -rf /")`` ever produced two
    ``shlex.split`` tokens, the attacker controls subsequent shell
    state. The property forces a single-token result regardless of how
    Hypothesis interleaves the metacharacter with benign text.
    """
    payload = f"{prefix}{injection}{suffix}"
    parts = shlex.split(shell_quote(payload))
    assert parts == [payload], (
        f"injection char {injection!r} escaped quoting: shlex.split({shell_quote(payload)!r}) = {parts!r}"
    )


@given(argv=_argv_strategy())
def test_join_idempotent_under_round_trip(argv: list[str]) -> None:
    """``shell_join`` is canonical: re-joining after a round-trip is stable.

    ``shell_join(shlex.split(shell_join(argv))) == shell_join(argv)``.
    Without this, a value that flows through a chain of quote-unquote
    operations would drift (e.g. a benchmark-command string read from
    config, parsed, then re-joined to log).
    """
    once = shell_join(argv)
    twice = shell_join(shlex.split(once))
    assert once == twice


@given(
    argv=st.lists(
        st.text(_TOKEN_ALPHABET, min_size=1, max_size=8)
        | st.builds(
            lambda a, b, c: f"{a}{b}{c}",
            st.text(_TOKEN_ALPHABET, max_size=4),
            _INJECTION_CHARS,
            st.text(_TOKEN_ALPHABET, max_size=4),
        ),
        min_size=1,
        max_size=4,
    ),
)
def test_argv_with_injection_chars_round_trips(argv: list[str]) -> None:
    """argv carrying metacharacters survives a full join+split.

    Composite of the two prior properties at the argv level: a
    Hypothesis-generated mix of benign tokens and injection-laden
    tokens must reassemble to the original argv.
    """
    # shlex collapses whitespace-only tokens; skip those to keep the
    # invariant well-defined.
    assume(all(tok.strip() != "" or tok == "" for tok in argv))
    joined = shell_join(argv)
    assert shlex.split(joined) == argv


@_FORCE_POSIX
def test_empty_string_explicit_quoting() -> None:
    """``shell_quote("")`` produces ``"''"`` (not empty).

    Pinned because the input space is a single value. Documents the
    contract for callers like ``ssh_backend.write_file`` that splice
    quoted tokens directly into a shell line.
    """
    quoted = shell_quote("")
    assert quoted == "''"
    assert shlex.split(quoted) == [""]


@patch("bernstein.core.config.platform_compat.IS_WINDOWS", True)
def test_shell_quote_windows_branch_wraps_metacharacters() -> None:
    """The Windows ``cmd.exe`` branch double-quotes hostile tokens.

    Runs on every host (the Windows branch is pure string logic once
    ``IS_WINDOWS`` is flipped), so the Windows quoting contract is pinned
    cross-platform rather than only on a Windows runner:

    * an empty string becomes ``'""'`` (never collapses away);
    * a token with a shell metacharacter is wrapped in double quotes with
      interior quotes and percent signs escaped; and
    * a benign bareword is returned unquoted.
    """
    assert shell_quote("") == '""'
    assert shell_quote("plain") == "plain"
    quoted = shell_quote('a & b "c" 100%')
    assert quoted.startswith('"') and quoted.endswith('"')
    assert '\\"' in quoted
    assert "%%" in quoted


@given(token=st.text(min_size=0, max_size=128))
def test_shell_quote_never_returns_empty(token: str) -> None:
    """``shell_quote`` never returns ``""``.

    A returned empty string would silently disappear in shell line
    splicing. The function's contract is to always return at least
    one character of quoting/data.
    """
    assert shell_quote(token) != ""
