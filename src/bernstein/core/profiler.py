"""cProfile integration for orchestrator bottleneck analysis.

Wraps the orchestrator main loop with cProfile when ``BERNSTEIN_PROFILE=1``
is set.  Writes a binary ``.prof`` file for ``pstats`` / SnakeViz and a
human-readable ``.txt`` report to ``.sdd/runtime/profiles/``.

Usage in the orchestrator ``__main__`` block::

    if os.environ.get("BERNSTEIN_PROFILE"):
        from bernstein.core.profiler import ProfilerSession
        output_dir = workdir / ".sdd" / "runtime" / "profiles"
        with ProfilerSession(output_dir):
            orchestrator.run()
    else:
        orchestrator.run()

The profile output path can be overridden via ``BERNSTEIN_PROFILE_OUTPUT``.
"""

from __future__ import annotations

import cProfile
import io
import logging
import pstats
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import TracebackType

logger = logging.getLogger(__name__)

#: Number of functions shown in the human-readable summary.
_TOP_N = 40


class ProfilerSession:
    """Context manager that profiles the wrapped code block with cProfile.

    On exit, writes two files to *output_dir*:
    - ``profile-{timestamp}.prof`` — binary pstats dump (open with SnakeViz)
    - ``profile-{timestamp}.txt`` — top-N functions by cumulative time

    Args:
        output_dir: Directory to write profile artifacts into.
        top_n: Number of functions to include in the text report.
    """

    def __init__(self, output_dir: Path, *, top_n: int = _TOP_N) -> None:
        self._output_dir = output_dir
        self._top_n = top_n
        self._profiler = cProfile.Profile()
        self._start_ts: float = 0.0

    def __enter__(self) -> ProfilerSession:
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._start_ts = time.monotonic()
        self._profiler.enable()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self._profiler.disable()
        elapsed = time.monotonic() - self._start_ts
        self._save(elapsed)

    def _save(self, elapsed_s: float) -> None:
        ts = time.strftime("%Y%m%d-%H%M%S")
        prof_path = self._output_dir / f"profile-{ts}.prof"
        txt_path = self._output_dir / f"profile-{ts}.txt"

        # Binary dump — readable by pstats, SnakeViz, pyinstrument
        self._profiler.dump_stats(str(prof_path))

        # Human-readable report
        buf = io.StringIO()
        ps = pstats.Stats(self._profiler, stream=buf)
        ps.strip_dirs().sort_stats("cumulative").print_stats(self._top_n)
        report = buf.getvalue()

        header = (
            f"# Bernstein orchestrator profile\n"
            f"# Elapsed: {elapsed_s:.2f}s\n"
            f"# Top {self._top_n} functions by cumulative time\n\n"
        )
        txt_path.write_text(header + report, encoding="utf-8")

        logger.info("Profile saved to %s", prof_path)
        logger.info("Profile report saved to %s", txt_path)
        print(f"\n[profiler] Elapsed: {elapsed_s:.2f}s")
        print(f"[profiler] Binary dump: {prof_path}")
        print(f"[profiler] Text report: {txt_path}")
        print(f"[profiler] Top {self._top_n} functions:\n{report}")


def resolve_profile_output_dir(workdir: Path) -> Path:
    """Return the profile output directory from env or default.

    Args:
        workdir: Project root directory.

    Returns:
        Path to the profile output directory.
    """
    import os

    override = os.environ.get("BERNSTEIN_PROFILE_OUTPUT", "")
    if override:
        return Path(override)
    return workdir / ".sdd" / "runtime" / "profiles"
