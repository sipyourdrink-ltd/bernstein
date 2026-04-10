"""Tests for WEB-022: Dashboard embedding support (iframe-friendly)."""

from __future__ import annotations

from pathlib import Path

from bernstein.core.routes.embedding import (
    EmbeddingConfig,
    build_csp_header,
    build_frame_options_header,
    load_embedding_config,
)


class TestEmbeddingConfig:
    def test_default_deny(self) -> None:
        config = EmbeddingConfig()
        assert config.allow_embedding is False
        assert config.allowed_origins == []

    def test_allow_all(self) -> None:
        config = EmbeddingConfig(allow_embedding=True)
        assert config.allow_embedding is True


class TestBuildHeaders:
    def test_deny_frame_options(self) -> None:
        config = EmbeddingConfig(allow_embedding=False)
        assert build_frame_options_header(config) == "DENY"

    def test_allow_any_frame_options(self) -> None:
        config = EmbeddingConfig(allow_embedding=True)
        # No X-Frame-Options when embedding is allowed from any origin
        assert build_frame_options_header(config) is None

    def test_specific_origins_frame_options(self) -> None:
        config = EmbeddingConfig(
            allow_embedding=True,
            allowed_origins=["https://example.com"],
        )
        assert build_frame_options_header(config) is None  # CSP handles this

    def test_deny_csp(self) -> None:
        config = EmbeddingConfig(allow_embedding=False)
        csp = build_csp_header(config)
        assert "frame-ancestors 'none'" in csp

    def test_allow_any_csp(self) -> None:
        config = EmbeddingConfig(allow_embedding=True)
        csp = build_csp_header(config)
        assert "frame-ancestors *" in csp

    def test_specific_origins_csp(self) -> None:
        config = EmbeddingConfig(
            allow_embedding=True,
            allowed_origins=["https://example.com", "https://notion.so"],
        )
        csp = build_csp_header(config)
        csp_parts = csp.split()
        assert "https://example.com" in csp_parts
        assert "https://notion.so" in csp_parts


class TestLoadConfig:
    def test_load_missing(self) -> None:
        config = load_embedding_config(Path("/nonexistent.yaml"))
        assert config == EmbeddingConfig()

    def test_load_from_yaml(self, tmp_path: Path) -> None:
        yaml_file = tmp_path / "bernstein.yaml"
        yaml_file.write_text(
            "embedding:\n  allow_embedding: true\n  allowed_origins:\n    - https://example.com\n",
            encoding="utf-8",
        )
        config = load_embedding_config(yaml_file)
        assert config.allow_embedding is True
        assert config.allowed_origins == ["https://example.com"]
