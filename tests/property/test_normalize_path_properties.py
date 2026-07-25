"""Property tests for path normalisation and traversal safety.

Bernstein has two related path safety surfaces:

1. ``bernstein.core.config.platform_compat.normalize_path`` - used to
   canonicalise operator-supplied paths before they hit subprocess
   args, gitignore patterns, etc. Bugs here surface as broken path
   matching on either platform.

2. ``bernstein.core.lineage.signed_write._is_unsafe_path`` - the
   defence-in-depth check that rejects absolute and traversal
   artefact paths before they reach disk. A regression here is a
   direct path-traversal vulnerability (the recorder could write
   outside the lineage store).

Properties:

* **``normalize_path`` is idempotent** - running it twice yields the
  same string. Catches regressions where the normaliser leaves a
  trailing separator on some inputs that a second pass would strip.

* **``normalize_path`` collapses ``"./"`` segments** - the contract
  that downstream gitignore-style matchers depend on.

* **``_is_unsafe_path`` rejects every traversal segment** - for any
  segment count and position, ``..`` anywhere in the path is
  detected.

* **Trusted relative paths are accepted** - pure-relative paths
  without traversal segments must pass cleanly; otherwise the
  recorder would refuse legitimate writes.

The lineage path-safety check is a pure function; the property runs
50 examples in well under a second.
"""

from __future__ import annotations

from hypothesis import assume, given
from hypothesis import strategies as st

from bernstein.core.config.platform_compat import normalize_path
from bernstein.core.lineage.signed_write import _is_unsafe_path

_SAFE_SEG = st.text(
    alphabet=st.characters(
        min_codepoint=0x61,
        max_codepoint=0x7A,  # lowercase ascii - boring, safe segments
    ),
    min_size=1,
    max_size=8,
)


@given(
    segments=st.lists(_SAFE_SEG, min_size=1, max_size=6),
    use_forward=st.booleans(),
)
def test_normalize_path_idempotent(segments: list[str], use_forward: bool) -> None:
    """``normalize_path(normalize_path(p)) == normalize_path(p)``.

    The normaliser is supposed to converge on a canonical form. A
    drift between two calls would surface as flaky gitignore matching
    in the dependency-scan plugin (and elsewhere).
    """
    sep = "/" if use_forward else "\\"
    raw = sep.join(segments)
    once = normalize_path(raw)
    twice = normalize_path(once)
    assert once == twice


@given(segments=st.lists(_SAFE_SEG, min_size=1, max_size=6))
def test_normalize_collapses_dot_segments(segments: list[str]) -> None:
    """``./`` segments are collapsed by normalisation.

    Catches regressions where the normaliser stops short of removing
    explicit ``.`` components (which causes Match-vs-Compare drift
    against any path that the writer canonicalised differently).
    """
    raw = "./" + "/".join(segments)
    normalised = normalize_path(raw)
    # ``./a/b`` → ``a/b``; the normaliser produces no leading ``./``.
    assert not normalised.startswith("./")


@given(
    prefix=st.lists(_SAFE_SEG, max_size=3),
    suffix=st.lists(_SAFE_SEG, max_size=3),
)
def test_traversal_segment_anywhere_is_unsafe(prefix: list[str], suffix: list[str]) -> None:
    """``..`` segment anywhere in the path is rejected.

    The recorder uses ``_is_unsafe_path`` as its only line of defence
    against path traversal; this property checks that the segment is
    detected regardless of position.
    """
    segments = [*prefix, "..", *suffix]
    raw = "/".join(segments)
    reason = _is_unsafe_path(raw)
    assert reason is not None
    assert "path traversal" in reason


@given(segments=st.lists(_SAFE_SEG, min_size=1, max_size=5))
def test_safe_relative_path_accepted(segments: list[str]) -> None:
    """Relative path with no ``..`` and no leading ``/`` is accepted.

    The complement of the previous property: legitimate paths must not
    be rejected. Catches over-broad regexes (e.g. matching ``."`` or
    ``..foo``).
    """
    # Filter out segments that themselves equal ``..`` (Hypothesis
    # respects the alphabet but ``..`` is a literal value that
    # ``_SAFE_SEG`` cannot generate; defensive ``assume`` anyway).
    assume(all(s != ".." for s in segments))
    raw = "/".join(segments)
    assert _is_unsafe_path(raw) is None


@given(path=_SAFE_SEG)
def test_absolute_paths_rejected(path: str) -> None:
    """Any leading-slash path is rejected by the recorder.

    Lineage paths are repo-relative POSIX strings; the recorder must
    refuse an absolute path or it would write outside the store.
    """
    raw = "/" + path
    reason = _is_unsafe_path(raw)
    assert reason is not None
    assert "absolute" in reason


def test_empty_path_rejected() -> None:
    """The empty path is rejected.

    Pinned (input space is a single value). Documents the contract
    so a future refactor cannot accidentally accept ``""``.
    """
    reason = _is_unsafe_path("")
    assert reason is not None
    assert "empty" in reason


@given(
    drive=st.sampled_from(["C", "D", "Z"]),
    rest=_SAFE_SEG,
)
def test_windows_absolute_paths_rejected(drive: str, rest: str) -> None:
    """Windows-style ``X:\\...`` paths are rejected on every platform.

    The recorder writes POSIX-style paths; a Windows-shaped value
    leaking through would either confuse the path resolver or
    (worse) be interpreted as ``X:`` plus a relative path on POSIX
    and silently land in the wrong directory.
    """
    raw = f"{drive}:\\{rest}"
    reason = _is_unsafe_path(raw)
    assert reason is not None
