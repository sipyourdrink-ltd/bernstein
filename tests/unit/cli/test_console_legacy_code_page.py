"""The CLI console survives a legacy code page (issue #3901).

``bernstein demo`` aborts on a Windows console using cp1252 because it prints
U+2713 and the stream's default ``errors="strict"`` turns an unencodable glyph
into a ``UnicodeEncodeError`` mid-command.

These render the glyph through a console whose stream really is cp1252 and
assert it does not raise. Asserting on the arguments ``make_console`` passes
would pass while ``run_confirm.py`` still died on the real stream, which is the
failure mode this file exists to prevent.
"""

from __future__ import annotations

import io

import pytest

from bernstein.cli.ui import make_console

_CHECK_MARK = "✓"


def _cp1252_stream() -> io.TextIOWrapper:
    """A text stream with the encoding a default Windows console reports."""
    return io.TextIOWrapper(io.BytesIO(), encoding="cp1252", newline="")


def test_cp1252_stream_cannot_encode_the_check_mark() -> None:
    """The control: without intervention the glyph really is fatal.

    If this ever stops raising, the tests below stop proving anything.
    """
    stream = _cp1252_stream()

    with pytest.raises(UnicodeEncodeError):
        stream.write(_CHECK_MARK)
        stream.flush()


def test_console_renders_a_check_mark_on_a_legacy_code_page() -> None:
    """The demo's first status line does not abort the command."""
    stream = _cp1252_stream()
    console = make_console(file=stream)

    console.print(f"[green]{_CHECK_MARK}[/green] Flask app with 4 intentional bugs created")
    stream.flush()

    written = stream.buffer.getvalue().decode("cp1252")  # type: ignore[attr-defined]
    assert "Flask app with 4 intentional bugs created" in written


def test_no_color_console_also_survives_a_legacy_code_page() -> None:
    """The ``--no-color`` path builds a different Console; it needs the same guarantee."""
    stream = _cp1252_stream()
    console = make_console(no_color=True, file=stream)

    console.print(f"{_CHECK_MARK} done")
    stream.flush()

    assert "done" in stream.buffer.getvalue().decode("cp1252")  # type: ignore[attr-defined]


def test_a_utf8_stream_keeps_the_glyph_intact() -> None:
    """Tolerating unencodable glyphs must not degrade a console that can render them."""
    stream = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", newline="")
    console = make_console(file=stream)

    console.print(_CHECK_MARK)
    stream.flush()

    assert _CHECK_MARK in stream.buffer.getvalue().decode("utf-8")  # type: ignore[attr-defined]


def test_surrogateescape_stream_is_still_made_tolerant() -> None:
    """The real case: Python gives stdout ``surrogateescape`` on Windows.

    That policy recovers lone surrogates and nothing else, so U+2713 raises
    under it exactly as under ``strict``. Treating it as "already tolerant"
    is what let the first attempt at this fix pass its tests while
    ``bernstein demo`` still died on the real stream.
    """
    stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="surrogateescape", newline="")
    console = make_console(file=stream)

    console.print(f"{_CHECK_MARK} seeded")
    stream.flush()

    assert "seeded" in stream.buffer.getvalue().decode("cp1252")  # type: ignore[attr-defined]


def test_a_stream_that_already_replaces_is_left_alone() -> None:
    """An explicit error policy set by the caller is not overridden."""
    stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="xmlcharrefreplace", newline="")

    make_console(file=stream)

    assert stream.errors == "xmlcharrefreplace"


def test_shared_cli_console_is_built_through_the_factory() -> None:
    """``helpers.console`` is what ``run_confirm.py`` prints through.

    A bare ``Console()`` there would reintroduce the defect no matter how the
    factory behaves, which is exactly how this bug reached a release.
    """
    from bernstein.cli import helpers

    assert (helpers.console.file.errors or "strict") != "strict"
