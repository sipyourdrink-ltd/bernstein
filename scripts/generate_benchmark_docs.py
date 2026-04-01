"""Generate the public benchmark docs page from SWE-Bench result artifacts."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from benchmarks.swe_bench.public_site import (  # noqa: E402
    build_public_context,
    load_summaries,
    render_public_html,
)

_DEFAULT_RESULTS_DIR = _REPO_ROOT / "benchmarks" / "swe_bench" / "results"
_DEFAULT_OUTPUT = _REPO_ROOT / "docs" / "leaderboard.html"


def main() -> int:
    """Render the public benchmark HTML page from saved summary artifacts."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=_DEFAULT_RESULTS_DIR,
        help="Directory containing SWE-Bench summary artifacts.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=_DEFAULT_OUTPUT,
        help="Path to write the generated docs HTML page.",
    )
    args = parser.parse_args()

    summaries = load_summaries(args.results_dir)
    context = build_public_context(summaries)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render_public_html(context), encoding="utf-8")
    print(f"Wrote benchmark docs page to: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
