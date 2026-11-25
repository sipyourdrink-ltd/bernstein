"""Edge-case coverage for the AST-aware reviewer chunker.

Complements ``test_ast_chunker.py`` (the happy-path 5000-line fixture) by
exercising the corners every reviewer pipeline trips over in practice:

* unsupported / mistyped extensions (Python AST path is single-language)
* malformed Python that should degrade gracefully, not crash
* binary / wrong-encoding files masquerading as ``.py``
* very large files, single oversize functions, many tiny units
* empty / whitespace / comment-only / single-newline files
* CRLF line endings, decorator stacks, async defs, nested classes
* directory paths, symlinks
* zero / negative / huge budget values
* line-based fallback applies uniformly to non-Python languages
  (JS, TS, Go, Rust, Ruby, YAML, Markdown, C, Java)
"""

from __future__ import annotations

import ast
import dataclasses
import itertools
import logging
from pathlib import Path

import pytest

from bernstein.core.quality.review_pipeline import (
    ReviewChunk,
    chunk_for_review,
)
from bernstein.core.quality.review_pipeline.ast_chunker import (
    _MIN_BUDGET_TOKENS,
    _UTF8_BOM,
    _normalize_budget,
)

LOGGER_NAME = "bernstein.core.quality.review_pipeline.ast_chunker"


# ---------------------------------------------------------------------------
# Encoding / byte-level corner cases
# ---------------------------------------------------------------------------


def test_utf8_bom_python_file_takes_ast_path(tmp_path: Path) -> None:
    """A leading UTF-8 BOM must not push a real Python file into line fallback."""
    fp = tmp_path / "bom.py"
    fp.write_bytes(b"\xef\xbb\xbfdef foo():\n    return 1\n")
    chunks = chunk_for_review(fp)
    assert chunks
    assert chunks[0].language == "python"
    assert "foo" in chunks[0].symbols


def test_utf16_python_file_returns_empty_with_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    fp = tmp_path / "u16.py"
    fp.write_bytes("def foo():\n    return 1\n".encode("utf-16"))
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        chunks = chunk_for_review(fp)
    assert chunks == []
    assert any("cannot read" in r.getMessage() for r in caplog.records)


def test_binary_file_with_py_extension_returns_empty(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A non-UTF-8 binary masquerading as .py must not crash the chunker."""
    fp = tmp_path / "binary.py"
    fp.write_bytes(bytes(range(256)))
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        chunks = chunk_for_review(fp)
    assert chunks == []
    assert any("cannot read" in r.getMessage() for r in caplog.records)


def test_crlf_line_endings_are_normalized(tmp_path: Path) -> None:
    """Windows-authored files round-trip through the AST path cleanly."""
    fp = tmp_path / "crlf.py"
    fp.write_bytes(b"def foo():\r\n    return 1\r\n\r\ndef bar():\r\n    return 2\r\n")
    chunks = chunk_for_review(fp)
    assert len(chunks) == 1
    assert chunks[0].symbols == ("foo", "bar")
    # Output text uses LF only - no stray CR bytes leak through.
    assert "\r" not in chunks[0].text


def test_constant_bom_value_matches_unicode_codepoint() -> None:
    assert _UTF8_BOM == "﻿"
    assert _UTF8_BOM.encode("utf-8") == b"\xef\xbb\xbf"


# ---------------------------------------------------------------------------
# Empty / sparse content
# ---------------------------------------------------------------------------


def test_empty_python_file_returns_empty(tmp_path: Path) -> None:
    fp = tmp_path / "empty.py"
    fp.write_text("", encoding="utf-8")
    assert chunk_for_review(fp) == []


def test_single_newline_only_python_file_returns_one_chunk(tmp_path: Path) -> None:
    fp = tmp_path / "nl.py"
    fp.write_text("\n", encoding="utf-8")
    chunks = chunk_for_review(fp)
    assert len(chunks) == 1
    assert chunks[0].symbols == ()
    assert chunks[0].language == "python"


def test_comments_only_python_file_yields_one_symbolless_chunk(tmp_path: Path) -> None:
    fp = tmp_path / "cmts.py"
    fp.write_text("# header\n# notes\n# tail\n", encoding="utf-8")
    chunks = chunk_for_review(fp)
    assert len(chunks) == 1
    assert chunks[0].symbols == ()
    assert "header" in chunks[0].text


def test_top_level_statements_only(tmp_path: Path) -> None:
    """Module-level statements without defs/classes still emit a single chunk."""
    fp = tmp_path / "stmts.py"
    fp.write_text("import os\n\nX = 1\nY = 2\n", encoding="utf-8")
    chunks = chunk_for_review(fp)
    assert len(chunks) == 1
    assert chunks[0].symbols == ()
    assert chunks[0].start_line == 1
    assert chunks[0].end_line >= 4


# ---------------------------------------------------------------------------
# Symbol detection - async defs, decorators, nested classes
# ---------------------------------------------------------------------------


def test_async_def_is_recognised_as_symbol(tmp_path: Path) -> None:
    fp = tmp_path / "async_mod.py"
    fp.write_text("async def afoo():\n    return 1\n", encoding="utf-8")
    chunks = chunk_for_review(fp)
    assert chunks[0].symbols == ("afoo",)


def test_decorator_stack_is_kept_with_function(tmp_path: Path) -> None:
    """Decorators belong to the function - chunk text must include them all."""
    fp = tmp_path / "deco.py"
    fp.write_text(
        "@staticmethod\n@property\ndef foo():\n    return 1\n",
        encoding="utf-8",
    )
    chunks = chunk_for_review(fp)
    assert len(chunks) == 1
    assert chunks[0].symbols == ("foo",)
    text = chunks[0].text
    assert "@staticmethod" in text
    assert "@property" in text
    assert text.index("@staticmethod") < text.index("def foo")


@pytest.mark.parametrize(
    ("decorators", "definition", "symbol"),
    [
        ("@outer\n@inner\n", "def measure():", "measure"),
        ("@register\n", "class Sample:", "Sample"),
        ("@(\n    wrapper\n)\n", "async def fetch():", "fetch"),
    ],
    ids=["stacked-function", "class", "multiline-async"],
)
def test_decorators_stay_with_oversized_definitions(
    tmp_path: Path, decorators: str, definition: str, symbol: str
) -> None:
    decorated = f"{decorators}{definition}\n    value = {'x' * 300!r}\n"
    source = "prefix = 1\n" + decorated
    path = tmp_path / "decorated.py"
    path.write_text(source, encoding="utf-8")

    chunks = chunk_for_review(path, budget_tokens=64)
    chunk = next(chunk for chunk in chunks if symbol in chunk.symbols)
    assert chunk.symbols == (symbol,)
    assert (chunk.start_line, chunk.end_line) == (2, len(source.splitlines()))
    assert chunk.text == decorated
    assert "".join(part.text for part in chunks) == source


def test_nested_classes_belong_to_outer_class_chunk(tmp_path: Path) -> None:
    """Inner classes are part of the outer class's AST node - not separate symbols."""
    fp = tmp_path / "nest.py"
    fp.write_text(
        "class Outer:\n    class Inner:\n        def m(self): return 1\n",
        encoding="utf-8",
    )
    chunks = chunk_for_review(fp)
    assert len(chunks) == 1
    assert chunks[0].symbols == ("Outer",)
    assert "class Inner" in chunks[0].text


def test_main_guard_is_not_a_top_level_symbol(tmp_path: Path) -> None:
    fp = tmp_path / "guard.py"
    fp.write_text(
        "def foo():\n    return 1\n\nif __name__ == '__main__':\n    foo()\n",
        encoding="utf-8",
    )
    chunks = chunk_for_review(fp)
    assert chunks[0].symbols == ("foo",)
    full_text = "".join(c.text for c in chunks)
    assert "__main__" in full_text


# ---------------------------------------------------------------------------
# Budget edge cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_budget", [-100, -1, 0, 1, 5])
def test_subminimum_budget_does_not_break_chunker(tmp_path: Path, bad_budget: int) -> None:
    """Negative / zero / tiny budgets clamp to the floor instead of crashing."""
    fp = tmp_path / "tiny.py"
    fp.write_text("def foo(): return 1\n", encoding="utf-8")
    chunks = chunk_for_review(fp, budget_tokens=bad_budget)
    assert chunks
    assert chunks[0].symbols == ("foo",)


def test_normalize_budget_clamps_below_floor() -> None:
    assert _normalize_budget(-5) == _MIN_BUDGET_TOKENS
    assert _normalize_budget(0) == _MIN_BUDGET_TOKENS
    assert _normalize_budget(_MIN_BUDGET_TOKENS) == _MIN_BUDGET_TOKENS
    # Sane values pass through untouched.
    assert _normalize_budget(4000) == 4000


def test_huge_single_function_stays_intact_under_tiny_budget(tmp_path: Path) -> None:
    """A function bigger than the budget must still be one whole chunk."""
    fp = tmp_path / "huge.py"
    body = "\n".join(f"    x += {i}" for i in range(2000))
    fp.write_text(f"def big():\n    x = 0\n{body}\n    return x\n", encoding="utf-8")
    chunks = chunk_for_review(fp, budget_tokens=10)
    assert len(chunks) == 1
    parsed = ast.parse(chunks[0].text)
    assert isinstance(parsed.body[0], ast.FunctionDef)
    assert parsed.body[0].name == "big"


def test_many_small_functions_produce_multiple_chunks(tmp_path: Path) -> None:
    """A flat module of dozens of tiny defs splits into multiple chunks at low budget."""
    fp = tmp_path / "many.py"
    src = "\n\n".join(f"def f{i}():\n    return {i}" for i in range(40)) + "\n"
    fp.write_text(src, encoding="utf-8")
    chunks = chunk_for_review(fp, budget_tokens=120)
    assert len(chunks) >= 2
    seen = [s for c in chunks for s in c.symbols]
    assert sorted(seen) == sorted(f"f{i}" for i in range(40))


# ---------------------------------------------------------------------------
# Filesystem corner cases
# ---------------------------------------------------------------------------


def test_directory_path_returns_empty_with_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        chunks = chunk_for_review(tmp_path)
    assert chunks == []
    assert any("cannot read" in r.getMessage() for r in caplog.records)


def test_symlink_to_python_file_is_chunked(tmp_path: Path) -> None:
    real = tmp_path / "real.py"
    real.write_text("def foo(): return 1\n", encoding="utf-8")
    link = tmp_path / "link.py"
    link.symlink_to(real)
    chunks = chunk_for_review(link)
    assert len(chunks) == 1
    # Path string preserves whatever the caller asked for.
    assert chunks[0].path == str(link)
    assert chunks[0].symbols == ("foo",)


def test_path_string_argument_is_preserved_verbatim(tmp_path: Path) -> None:
    fp = tmp_path / "preserve.py"
    fp.write_text("def foo(): return 1\n", encoding="utf-8")
    chunks_str = chunk_for_review(str(fp))
    chunks_path = chunk_for_review(fp)
    assert chunks_str[0].path == str(fp)
    assert chunks_path[0].path == str(fp)


# ---------------------------------------------------------------------------
# Non-Python language fallback (current contract: line-based, no AST)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ext", "src"),
    [
        ("js", "function foo() { return 1; }\nfunction bar() { return 2; }\n"),
        ("ts", "export function foo(): number { return 1; }\n"),
        ("go", "package main\n\nfunc Foo() int { return 1 }\n"),
        ("rs", "pub fn foo() -> i32 { 1 }\n\npub fn bar() -> i32 { 2 }\n"),
        ("rb", "def foo\n  1\nend\n\ndef bar\n  2\nend\n"),
        ("yaml", "version: 1\nstages:\n  - name: build\n  - name: test\n"),
        ("md", "# Heading\n\nBody paragraph.\n\n* item one\n* item two\n"),
        ("c", "int foo(void) { return 1; }\n"),
        ("java", "class A { int foo() { return 1; } }\n"),
    ],
)
def test_non_python_languages_use_line_based_fallback(
    tmp_path: Path, ext: str, src: str, caplog: pytest.LogCaptureFixture
) -> None:
    fp = tmp_path / f"sample.{ext}"
    fp.write_text(src, encoding="utf-8")
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        chunks = chunk_for_review(fp, budget_tokens=4000)
    assert chunks
    assert all(c.language == "text" for c in chunks)
    assert all(c.symbols == () for c in chunks)
    assert any("not Python" in r.getMessage() and "line-based fallback" in r.getMessage() for r in caplog.records)


def test_line_based_fallback_covers_all_lines_in_order(tmp_path: Path) -> None:
    """The fallback windowing must walk the file in 1-indexed contiguous order."""
    fp = tmp_path / "log.txt"
    total = 750
    fp.write_text("\n".join(f"line {i}" for i in range(1, total + 1)) + "\n", encoding="utf-8")
    chunks = chunk_for_review(fp, budget_tokens=400)
    assert chunks[0].start_line == 1
    assert chunks[-1].end_line >= total
    for prev, nxt in itertools.pairwise(chunks):
        assert nxt.start_line == prev.end_line + 1
        assert prev.end_line >= prev.start_line


# ---------------------------------------------------------------------------
# Malformed Python - graceful degradation, not crash
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "src",
    [
        "def oops(:\n    pass\n",
        "class Broken\n    def m(self): pass\n",
        "x = (1 + (2 + (3\n",
        "def f(:\nreturn 1",
    ],
)
def test_malformed_python_falls_back_to_line_based(tmp_path: Path, src: str, caplog: pytest.LogCaptureFixture) -> None:
    fp = tmp_path / "bad.py"
    fp.write_text(src, encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        chunks = chunk_for_review(fp, budget_tokens=2000)
    assert chunks
    assert all(c.language == "text" for c in chunks)


# ---------------------------------------------------------------------------
# Many top-level units in a single file - output ordering & coverage
# ---------------------------------------------------------------------------


def test_chunks_cover_every_top_level_symbol(tmp_path: Path) -> None:
    fp = tmp_path / "wide.py"
    parts: list[str] = []
    for i in range(60):
        parts.append(f"def helper_{i}(x: int) -> int:\n    return x + {i}\n")
        if i % 6 == 5:
            parts.append(f"class Thing{i}:\n    value = {i}\n")
    fp.write_text("\n".join(parts) + "\n", encoding="utf-8")
    chunks = chunk_for_review(fp, budget_tokens=300)
    seen = {sym for c in chunks for sym in c.symbols}
    assert {f"helper_{i}" for i in range(60)} <= seen
    # Every chunk we emit must AST-parse cleanly - never a half-def.
    for c in chunks:
        ast.parse(c.text)


def test_review_chunk_dataclass_is_frozen() -> None:
    chunk = ReviewChunk(
        path="x.py",
        start_line=1,
        end_line=2,
        symbols=("foo",),
        text="def foo(): return 1\n",
        header="# x.py L1-2 - symbols: foo",
    )
    # Frozen dataclasses raise FrozenInstanceError on attribute assignment.
    with pytest.raises(dataclasses.FrozenInstanceError):
        chunk.start_line = 99  # type: ignore[misc]
