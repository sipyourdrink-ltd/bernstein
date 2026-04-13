"""Unit tests for compression model dataclasses."""

from __future__ import annotations

from dataclasses import asdict

import pytest
from bernstein.core.compression_models import CompressionMetrics, CompressionResult


def test_compression_metrics_creation() -> None:
    metrics = CompressionMetrics(
        bm25_matches=5,
        dependency_matches=3,
        semantic_matches=2,
        total_files_analyzed=20,
    )

    assert metrics.bm25_matches == 5
    assert metrics.total_files_analyzed == 20


def test_compression_result_round_trip_to_dict() -> None:
    metrics = CompressionMetrics(1, 2, 3, 10)
    result = CompressionResult(
        original_tokens=1000,
        compressed_tokens=600,
        compression_ratio=0.6,
        selected_files=["src/a.py", "src/b.py"],
        dropped_files=["src/c.py"],
        metrics=metrics,
    )

    payload = asdict(result)
    assert payload["compression_ratio"] == pytest.approx(0.6)
    assert payload["metrics"]["semantic_matches"] == 3


def test_compression_result_supports_empty_file_lists() -> None:
    result = CompressionResult(
        original_tokens=0,
        compressed_tokens=0,
        compression_ratio=0.0,
        selected_files=[],
        dropped_files=[],
        metrics=CompressionMetrics(0, 0, 0, 0),
    )

    assert result.selected_files == []
    assert result.dropped_files == []
