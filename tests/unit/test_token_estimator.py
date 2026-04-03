"""Tests for T803 — file-type-aware token estimation."""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.token_estimator import (
    CODE_BYTES_PER_TOKEN,
    DEFAULT_BYTES_PER_TOKEN,
    JSON_BYTES_PER_TOKEN,
    MINIFIED_BYTES_PER_TOKEN,
    TEXT_BYTES_PER_TOKEN,
    bytes_per_token_for_file_type,
    estimate_tokens_for_file_size,
    estimate_tokens_for_text,
)

# ---------------------------------------------------------------------------
# bytes_per_token_for_file_type
# ---------------------------------------------------------------------------


class TestBytesPerTokenForFileType:
    """Verify BPT selection across file categories."""

    # --- JSON / data files ---

    @pytest.mark.parametrize(
        "filename",
        ["config.json", "data.jsonl", "settings.jsonc"],
    )
    def test_json_files(self, filename: str) -> None:
        assert bytes_per_token_for_file_type(filename) == JSON_BYTES_PER_TOKEN

    @pytest.mark.parametrize("filename", ["config.yaml", "settings.yml", "project.toml"])
    def test_yaml_toml(self, filename: str) -> None:
        assert bytes_per_token_for_file_type(filename) == JSON_BYTES_PER_TOKEN

    # --- Source code files ---

    @pytest.mark.parametrize(
        "filename",
        [
            "server.py",
            "main.go",
            "app.ts",
            "component.tsx",
            "lib.rs",
            "Main.java",
            "handler.rb",
            "script.sh",
        ],
    )
    def test_code_files(self, filename: str) -> None:
        assert bytes_per_token_for_file_type(filename) == CODE_BYTES_PER_TOKEN

    # --- Text / prose files ---

    @pytest.mark.parametrize("filename", ["README.md", "notes.txt", "report.rst", "log.txt"])
    def test_text_files(self, filename: str) -> None:
        assert bytes_per_token_for_file_type(filename) == TEXT_BYTES_PER_TOKEN

    # --- Minified files ---

    def test_minified_js(self) -> None:
        assert bytes_per_token_for_file_type("bundle.min.js") == MINIFIED_BYTES_PER_TOKEN

    def test_minified_css(self) -> None:
        assert bytes_per_token_for_file_type("style.min.css") == MINIFIED_BYTES_PER_TOKEN

    # --- Binary files ---

    @pytest.mark.parametrize(
        "filename",
        ["image.png", "report.pdf", "archive.zip", "model.pkl", "db.sqlite"],
    )
    def test_binary_files_returns_none(self, filename: str) -> None:
        assert bytes_per_token_for_file_type(filename) is None

    # --- Special filenames ---

    @pytest.mark.parametrize("filename", ["Makefile", "Dockerfile", ".gitignore", "LICENSE"])
    def test_special_filenames(self, filename: str) -> None:
        assert bytes_per_token_for_file_type(filename) == TEXT_BYTES_PER_TOKEN

    # --- Fallback for unknown extensions ---

    def test_unknown_extension_returns_default(self) -> None:
        assert bytes_per_token_for_file_type("foo.xyz") == DEFAULT_BYTES_PER_TOKEN

    # --- Path objects ---

    def test_pathlib_path_accepted(self, tmp_path: Path) -> None:
        p = tmp_path / "module.py"
        assert bytes_per_token_for_file_type(p) == CODE_BYTES_PER_TOKEN

    # --- Subdirectory paths ---

    def test_subdirectory_path(self) -> None:
        assert bytes_per_token_for_file_type("src/data/config.json") == JSON_BYTES_PER_TOKEN


# ---------------------------------------------------------------------------
# estimate_tokens_for_file_size
# ---------------------------------------------------------------------------


class TestEstimateTokensForFileSize:
    """Verify token estimation given a file size in bytes."""

    def test_json_estimates_more_tokens_per_byte(self) -> None:
        """JSON at 2 bytes/token should yield more tokens than code at 4."""
        json_tokens = estimate_tokens_for_file_size("data.json", 8000)
        code_tokens = estimate_tokens_for_file_size("server.py", 8000)
        assert json_tokens > code_tokens

    def test_exact_json_estimation(self) -> None:
        assert estimate_tokens_for_file_size("data.json", 8000) == 4000

    def test_exact_code_estimation(self) -> None:
        assert estimate_tokens_for_file_size("server.py", 8000) == 2000

    def test_exact_text_estimation(self) -> None:
        assert estimate_tokens_for_file_size("notes.txt", 4000) == 1000

    def test_binary_returns_zero(self) -> None:
        assert estimate_tokens_for_file_size("image.png", 8000) == 0

    def test_zero_size(self) -> None:
        assert estimate_tokens_for_file_size("server.py", 0) == 0

    def test_minified_fewer_tokens(self) -> None:
        minified = estimate_tokens_for_file_size("bundle.min.js", 12000)
        normal = estimate_tokens_for_file_size("bundle.js", 12000)
        assert minified < normal


# ---------------------------------------------------------------------------
# estimate_tokens_for_text
# ---------------------------------------------------------------------------


class TestEstimateTokensForText:
    """Verify text-based token estimation."""

    def test_code_type(self) -> None:
        # 4 bytes/token → 800 bytes / 4 = 200 tokens (ascii)
        text = "x" * 800
        assert estimate_tokens_for_text(text, "code") == 200

    def test_json_type(self) -> None:
        text = "x" * 400
        assert estimate_tokens_for_text(text, "json") == 200

    def test_default_type_is_code(self) -> None:
        text = "x" * 800
        assert estimate_tokens_for_text(text) == estimate_tokens_for_text(text, "code")

    def test_unknown_type_falls_back(self) -> None:
        text = "x" * 800
        # Unknown assumed_type should use DEFAULT_BYTES_PER_TOKEN
        assert estimate_tokens_for_text(text, "unknown_category") == int(800 / DEFAULT_BYTES_PER_TOKEN)

    def test_multibyte_utf8(self) -> None:
        # Non-ASCII chars may take more bytes
        text = "日本語"  # 9 bytes in UTF-8
        assert estimate_tokens_for_text(text, "code") > 0
