"""Generate a public-safe markdown report from SWE-Bench result artifacts."""

from __future__ import annotations

from typing import TYPE_CHECKING

from benchmarks.swe_bench.public_site import build_public_context, load_summaries, render_public_markdown

if TYPE_CHECKING:
    from pathlib import Path


def generate_from_results_dir(
    results_dir: Path,
    output_path: Path | None = None,
    is_mock: bool | None = None,
) -> Path:
    """Load summaries from *results_dir* and render the public markdown report.

    The ``is_mock`` argument is kept for backward compatibility but no longer
    affects rendering. Provenance comes from summary metadata instead.
    """
    del is_mock

    if output_path is None:
        output_path = results_dir / "report.md"

    summaries = load_summaries(results_dir)
    context = build_public_context(summaries)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_public_markdown(context), encoding="utf-8")
    return output_path
