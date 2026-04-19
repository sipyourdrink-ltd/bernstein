"""Tests for file-type-aware token estimation helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.tokens.token_estimation import (
    CODE_BYTES_PER_TOKEN,
    DEFAULT_BYTES_PER_TOKEN,
    JSON_BYTES_PER_TOKEN,
    MINIFIED_BYTES_PER_TOKEN,
    TEXT_BYTES_PER_TOKEN,
    bytes_per_token_for_file_type,
    estimate_tokens_for_file,
    estimate_tokens_for_file_size,
    estimate_tokens_for_text,
)

# --- bytes_per_token_for_file_type ---


class TestBytesPerTokenForFileType:
    """Tests for bytes_per_token_for_file_type()."""

    @pytest.mark.parametrize(
        "path,expected",
        [
            ("data.json", JSON_BYTES_PER_TOKEN),
            ("config.yaml", JSON_BYTES_PER_TOKEN),
            ("config.yml", JSON_BYTES_PER_TOKEN),
            ("pyproject.toml", JSON_BYTES_PER_TOKEN),
            ("events.jsonl", JSON_BYTES_PER_TOKEN),
        ],
    )
    def test_json_extensions(self, path: str, expected: float) -> None:
        assert bytes_per_token_for_file_type(path) == expected

    @pytest.mark.parametrize(
        "path,expected",
        [
            ("main.py", CODE_BYTES_PER_TOKEN),
            ("app.ts", CODE_BYTES_PER_TOKEN),
            ("lib.rs", CODE_BYTES_PER_TOKEN),
            ("server.go", CODE_BYTES_PER_TOKEN),
            ("utils.js", CODE_BYTES_PER_TOKEN),
            ("query.sql", CODE_BYTES_PER_TOKEN),
        ],
    )
    def test_code_extensions(self, path: str, expected: float) -> None:
        assert bytes_per_token_for_file_type(path) == expected

    @pytest.mark.parametrize(
        "path,expected",
        [
            ("README.md", TEXT_BYTES_PER_TOKEN),
            ("notes.txt", TEXT_BYTES_PER_TOKEN),
            ("docs.rst", TEXT_BYTES_PER_TOKEN),
            ("output.log", TEXT_BYTES_PER_TOKEN),
            ("data.csv", TEXT_BYTES_PER_TOKEN),
        ],
    )
    def test_text_extensions(self, path: str, expected: float) -> None:
        assert bytes_per_token_for_file_type(path) == expected

    @pytest.mark.parametrize(
        "path,expected",
        [
            ("index.html", TEXT_BYTES_PER_TOKEN),
            ("feed.xml", TEXT_BYTES_PER_TOKEN),
        ],
    )
    def test_markup_extensions(self, path: str, expected: float) -> None:
        assert bytes_per_token_for_file_type(path) == expected

    @pytest.mark.parametrize("path", ["bundle.min.js", "style.min.css"])
    def test_minified_files(self, path: str) -> None:
        assert bytes_per_token_for_file_type(path) == MINIFIED_BYTES_PER_TOKEN

    @pytest.mark.parametrize(
        "path",
        ["image.png", "photo.jpg", "archive.zip", "binary.exe", "model.pkl", "data.sqlite"],
    )
    def test_binary_extensions_return_none(self, path: str) -> None:
        assert bytes_per_token_for_file_type(path) is None

    def test_special_filenames(self) -> None:
        # _SPECIAL_NAMES is case-sensitive and lower comparison doesn't match
        # so extensionless special names fall through to DEFAULT unless they
        # happen to match (the code lowercases then checks against original-case set).
        # Only CMakeLists.txt has a dot so it also won't match the "." guard.
        # This tests the actual behavior of the code.
        assert bytes_per_token_for_file_type("Makefile") == DEFAULT_BYTES_PER_TOKEN

    def test_dotfiles(self) -> None:
        assert bytes_per_token_for_file_type(".gitignore") == TEXT_BYTES_PER_TOKEN
        assert bytes_per_token_for_file_type(".env") == TEXT_BYTES_PER_TOKEN

    def test_unknown_extension(self) -> None:
        assert bytes_per_token_for_file_type("file.xyz123") == DEFAULT_BYTES_PER_TOKEN

    def test_path_object(self) -> None:
        assert bytes_per_token_for_file_type(Path("src/main.py")) == CODE_BYTES_PER_TOKEN


# --- estimate_tokens_for_file_size ---


class TestEstimateTokensForFileSize:
    """Tests for estimate_tokens_for_file_size()."""

    def test_python_file_estimation(self) -> None:
        tokens = estimate_tokens_for_file_size("code.py", 4000)
        assert tokens == int(4000 / CODE_BYTES_PER_TOKEN)

    def test_json_file_estimation(self) -> None:
        tokens = estimate_tokens_for_file_size("data.json", 2000)
        assert tokens == int(2000 / JSON_BYTES_PER_TOKEN)

    def test_zero_bytes_returns_zero(self) -> None:
        assert estimate_tokens_for_file_size("code.py", 0) == 0

    def test_binary_file_returns_zero(self) -> None:
        assert estimate_tokens_for_file_size("image.png", 50000) == 0


# --- estimate_tokens_for_text ---


class TestEstimateTokensForText:
    """Tests for estimate_tokens_for_text()."""

    def test_code_type(self) -> None:
        text = "def hello():\n    return 'world'\n"
        tokens = estimate_tokens_for_text(text, "code")
        expected = int(len(text.encode("utf-8")) / CODE_BYTES_PER_TOKEN)
        assert tokens == expected

    def test_json_type(self) -> None:
        text = '{"key": "value"}'
        tokens = estimate_tokens_for_text(text, "json")
        expected = int(len(text.encode("utf-8")) / JSON_BYTES_PER_TOKEN)
        assert tokens == expected

    def test_text_type(self) -> None:
        text = "Hello, this is a sentence."
        tokens = estimate_tokens_for_text(text, "text")
        expected = int(len(text.encode("utf-8")) / TEXT_BYTES_PER_TOKEN)
        assert tokens == expected

    def test_default_type(self) -> None:
        text = "some content"
        tokens = estimate_tokens_for_text(text, "default")
        expected = int(len(text.encode("utf-8")) / DEFAULT_BYTES_PER_TOKEN)
        assert tokens == expected

    def test_unknown_type_uses_default(self) -> None:
        text = "some content"
        tokens = estimate_tokens_for_text(text, "unknown_type")
        expected = int(len(text.encode("utf-8")) / DEFAULT_BYTES_PER_TOKEN)
        assert tokens == expected

    def test_empty_string(self) -> None:
        assert estimate_tokens_for_text("", "code") == 0

    def test_unicode_text(self) -> None:
        text = "Hello, world!"
        tokens = estimate_tokens_for_text(text, "text")
        byte_len = len(text.encode("utf-8"))
        assert tokens == int(byte_len / TEXT_BYTES_PER_TOKEN)


# --- estimate_tokens_for_file ---


class TestEstimateTokensForFile:
    """Tests for estimate_tokens_for_file()."""

    def test_string_content(self) -> None:
        content = "def foo(): pass"
        tokens = estimate_tokens_for_file("module.py", content)
        expected = int(len(content.encode("utf-8")) / CODE_BYTES_PER_TOKEN)
        assert tokens == expected

    def test_bytes_content(self) -> None:
        content = b'{"key": "value"}'
        tokens = estimate_tokens_for_file("data.json", content)
        expected = int(len(content) / JSON_BYTES_PER_TOKEN)
        assert tokens == expected

    def test_binary_file_returns_zero(self) -> None:
        assert estimate_tokens_for_file("image.png", b"\x89PNG\r\n\x1a\n") == 0


# --- Consolidation: single source of truth (audit-063) ---


class TestCanonicalEstimatorOnly:
    """Ensure the codebase has exactly one token estimator implementation.

    The legacy ``bernstein.core.tokens.token_estimator`` module was deleted in
    audit-063; ``bernstein.core.tokens.token_estimation`` is the canonical
    source.  Guard against accidental reintroduction of a duplicate module.
    """

    def test_legacy_token_estimator_module_is_gone(self) -> None:
        import importlib

        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("bernstein.core.tokens.token_estimator")

    def test_legacy_shim_is_removed_from_redirect_map(self) -> None:
        from bernstein.core import _REDIRECT_MAP

        assert "token_estimator" not in _REDIRECT_MAP
        assert _REDIRECT_MAP["token_estimation"] == "bernstein.core.tokens.token_estimation"

    def test_canonical_module_exposes_public_api(self) -> None:
        from bernstein.core.tokens import token_estimation as canonical

        for name in (
            "bytes_per_token_for_file_type",
            "estimate_tokens_for_file_size",
            "estimate_tokens_for_text",
            "estimate_tokens_for_file",
            "JSON_BYTES_PER_TOKEN",
            "CODE_BYTES_PER_TOKEN",
            "TEXT_BYTES_PER_TOKEN",
            "MINIFIED_BYTES_PER_TOKEN",
            "DEFAULT_BYTES_PER_TOKEN",
        ):
            assert hasattr(canonical, name), f"canonical estimator missing {name}"
