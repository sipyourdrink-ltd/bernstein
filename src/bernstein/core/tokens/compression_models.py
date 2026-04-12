"""Data models for context compression results and metrics.

Attributes and types used by the context compression engine to track
file selection, token reduction, and compression effectiveness.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CompressionMetrics:
    """Metrics from a single compression run.

    Attributes:
        bm25_matches: Number of files matched by BM25 keyword scoring.
        dependency_matches: Number of files matched via dependency graph.
        semantic_matches: Number of files matched by semantic similarity.
        total_files_analyzed: Total files in the project.
    """

    bm25_matches: int
    dependency_matches: int
    semantic_matches: int
    total_files_analyzed: int


@dataclass
class CompressionResult:
    """Result of a single context compression run.

    Attributes:
        original_tokens: Estimated tokens in full context.
        compressed_tokens: Estimated tokens in compressed context.
        compression_ratio: Ratio of compressed to original (e.g., 0.60 = 40% reduction).
        selected_files: List of relative file paths included in compressed context.
        dropped_files: List of relative file paths excluded from compressed context.
        metrics: Compression metrics (match counts, etc.).
    """

    original_tokens: int
    compressed_tokens: int
    compression_ratio: float
    selected_files: list[str]
    dropped_files: list[str]
    metrics: CompressionMetrics
