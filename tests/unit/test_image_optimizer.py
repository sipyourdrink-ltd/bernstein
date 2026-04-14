"""Tests for bernstein.core.tokens.image_optimizer."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bernstein.core.tokens.image_optimizer import (
    ImageCleanupResult,
    clean_images_from_context,
    estimate_image_tokens,
    render_image_report,
    scan_for_image_waste,
    should_strip_image,
)

# ---------------------------------------------------------------------------
# estimate_image_tokens
# ---------------------------------------------------------------------------


class TestEstimateImageTokens:
    """Token estimation from base64 length."""

    def test_empty(self) -> None:
        assert estimate_image_tokens("") == 0

    def test_small(self) -> None:
        assert estimate_image_tokens("AAAA") == 1

    def test_typical(self) -> None:
        data = "A" * 4000
        assert estimate_image_tokens(data) == 1000

    def test_odd_length(self) -> None:
        data = "A" * 5
        assert estimate_image_tokens(data) == 1  # 5 // 4


# ---------------------------------------------------------------------------
# should_strip_image
# ---------------------------------------------------------------------------


class TestShouldStripImage:
    """Image age check against keep_last."""

    def test_recent_kept(self) -> None:
        assert should_strip_image(0) is False
        assert should_strip_image(1) is False
        assert should_strip_image(2) is False

    def test_old_stripped(self) -> None:
        assert should_strip_image(3) is True
        assert should_strip_image(10) is True

    def test_custom_keep_last(self) -> None:
        assert should_strip_image(3, keep_last=5) is False
        assert should_strip_image(6, keep_last=5) is True


# ---------------------------------------------------------------------------
# clean_images_from_context
# ---------------------------------------------------------------------------


def _make_image_block(data: str = "AAAA" * 100) -> dict[str, Any]:
    """Create an Anthropic-style image content block."""
    return {"type": "image", "source": {"type": "base64", "data": data}}


def _make_openai_image_block(data: str = "AAAA" * 100) -> dict[str, Any]:
    """Create an OpenAI-style image_url content block."""
    return {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}}


def _make_text_block(text: str = "hello") -> dict[str, Any]:
    return {"type": "text", "text": text}


class TestCleanImagesFromContext:
    """Image cleanup from conversation messages."""

    def test_no_images(self) -> None:
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        result = clean_images_from_context(messages)
        assert result.images_found == 0
        assert result.images_removed == 0

    def test_all_images_recent(self) -> None:
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": [_make_image_block()]},
            {"role": "assistant", "content": [_make_text_block()]},
        ]
        result = clean_images_from_context(messages, keep_last=5)
        assert result.images_found == 1
        assert result.images_removed == 0
        assert result.kept_recent == 1

    def test_old_images_stripped(self) -> None:
        data = "B" * 400
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": [_make_image_block(data)]},
            {"role": "assistant", "content": [_make_text_block()]},
            {"role": "user", "content": [_make_text_block()]},
            {"role": "assistant", "content": [_make_text_block()]},
            {"role": "user", "content": [_make_image_block(data)]},
        ]
        result = clean_images_from_context(messages, keep_last=2)
        assert result.images_found == 2
        assert result.images_removed == 1  # first image is old
        assert result.kept_recent == 1  # last image is recent
        assert result.tokens_saved_estimate == len(data) // 4

        # Verify the old image was replaced with placeholder.
        first_content = messages[0]["content"]
        assert first_content[0]["type"] == "text"
        assert "removed" in first_content[0]["text"]

    def test_openai_format(self) -> None:
        data = "C" * 800
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": [_make_openai_image_block(data)]},
            {"role": "assistant", "content": [_make_text_block()]},
            {"role": "user", "content": [_make_text_block()]},
            {"role": "assistant", "content": [_make_text_block()]},
        ]
        result = clean_images_from_context(messages, keep_last=1)
        assert result.images_found == 1
        assert result.images_removed == 1
        assert result.tokens_saved_estimate == len(data) // 4

    def test_mixed_content(self) -> None:
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": [
                    _make_text_block("look at this"),
                    _make_image_block("D" * 200),
                    _make_text_block("what do you see?"),
                ],
            },
            {"role": "assistant", "content": [_make_text_block()]},
            {"role": "user", "content": [_make_text_block()]},
            {"role": "assistant", "content": [_make_text_block()]},
        ]
        result = clean_images_from_context(messages, keep_last=1)
        assert result.images_removed == 1
        # Text blocks should be preserved.
        content = messages[0]["content"]
        texts = [b["text"] for b in content if b["type"] == "text"]
        assert "look at this" in texts
        assert "what do you see?" in texts

    def test_none_content_skipped(self) -> None:
        messages: list[dict[str, Any]] = [
            {"role": "assistant", "content": None},
        ]
        result = clean_images_from_context(messages)
        assert result.images_found == 0

    def test_string_content_skipped(self) -> None:
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": "plain text"},
        ]
        result = clean_images_from_context(messages)
        assert result.images_found == 0


# ---------------------------------------------------------------------------
# scan_for_image_waste
# ---------------------------------------------------------------------------


class TestScanForImageWaste:
    """Scanning session directories for image waste."""

    def test_nonexistent_dir(self, tmp_path: Path) -> None:
        result = scan_for_image_waste(tmp_path / "nope")
        assert result["total_image_bytes"] == 0
        assert result["files_scanned"] == 0

    def test_empty_dir(self, tmp_path: Path) -> None:
        result = scan_for_image_waste(tmp_path)
        assert result["files_scanned"] == 0

    def test_detects_base64_in_logs(self, tmp_path: Path) -> None:
        log = tmp_path / "session.log"
        # Write a large base64 blob.
        blob = "A" * 1000
        log.write_text(f"some log data\ndata:image/png;base64,{blob}\nmore data")

        result = scan_for_image_waste(tmp_path)
        assert result["files_scanned"] == 1
        assert result["total_image_bytes"] > 0
        assert result["estimated_tokens"] > 0

    def test_recommendation_thresholds(self, tmp_path: Path) -> None:
        log = tmp_path / "big.log"
        # >50k tokens = >200k bytes of base64
        blob = "A" * 300_000
        log.write_text(f"data:image/png;base64,{blob}")

        result = scan_for_image_waste(tmp_path)
        assert "High" in result["recommendation"]


# ---------------------------------------------------------------------------
# render_image_report
# ---------------------------------------------------------------------------


class TestRenderImageReport:
    """Markdown report rendering."""

    def test_with_removals(self) -> None:
        result = ImageCleanupResult(
            images_found=5,
            images_removed=3,
            tokens_saved_estimate=12000,
            kept_recent=2,
        )
        report = render_image_report(result)
        assert "## Image Cleanup Report" in report
        assert "12,000" in report
        assert "3" in report

    def test_no_images(self) -> None:
        result = ImageCleanupResult(images_found=0, images_removed=0, tokens_saved_estimate=0, kept_recent=0)
        report = render_image_report(result)
        assert "No images found" in report

    def test_all_kept(self) -> None:
        result = ImageCleanupResult(images_found=2, images_removed=0, tokens_saved_estimate=0, kept_recent=2)
        report = render_image_report(result)
        assert "within the keep window" in report


# ---------------------------------------------------------------------------
# ImageCleanupResult frozen
# ---------------------------------------------------------------------------


class TestImageCleanupResultFrozen:
    """ImageCleanupResult is a frozen dataclass."""

    def test_frozen(self) -> None:
        result = ImageCleanupResult(images_found=1, images_removed=0, tokens_saved_estimate=0, kept_recent=1)
        with pytest.raises(AttributeError):
            result.images_found = 5  # type: ignore[misc]
