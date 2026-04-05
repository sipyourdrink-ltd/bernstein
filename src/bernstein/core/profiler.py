"""cProfile integration for orchestrator bottleneck analysis.

Wraps the orchestrator main loop with cProfile when ``BERNSTEIN_PROFILE=1``
is set.  Writes a binary ``.prof`` file for ``pstats`` / SnakeViz and a
human-readable ``.txt`` report to ``.sdd/runtime/profiles/``.

Provides two APIs:

- ``ProfilerSession`` — context manager that wraps a code block
- ``OrchestratorProfiler`` — explicit start/stop with ``ProfileResult``

Usage (context manager)::

    if os.environ.get("BERNSTEIN_PROFILE"):
        from bernstein.core.profiler import ProfilerSession
        output_dir = workdir / ".sdd" / "runtime" / "profiles"
        with ProfilerSession(output_dir):
            orchestrator.run()
    else:
        orchestrator.run()

Usage (explicit)::

    profiler = OrchestratorProfiler(output_dir)
    profiler.start()
    orchestrator.run()
    result = profiler.stop()
    print(profiler.to_markdown(result))

The profile output path can be overridden via ``BERNSTEIN_PROFILE_OUTPUT``.
"""

from __future__ import annotations

import cProfile
import io
import logging
import pstats
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import TracebackType

logger = logging.getLogger(__name__)

#: Number of functions shown in the human-readable summary.
_TOP_N = 40


@dataclass(frozen=True)
class ProfileResult:
    """Summary of a profiling run.

    Attributes:
        total_time: Wall-clock seconds the profiled block ran.
        top_functions: Top N functions sorted by cumulative time.
            Each entry is ``(name, cumtime, calls)``.
        output_path: Path to the binary ``.prof`` file (or ``None`` if not saved).
    """

    total_time: float
    top_functions: list[tuple[str, float, int]] = field(default_factory=list)
    output_path: Path | None = None


class OrchestratorProfiler:
    """Explicit start/stop profiler for orchestrator bottleneck analysis.

    Unlike ``ProfilerSession`` (a context manager), this class lets callers
    control profiling with explicit ``start()`` and ``stop()`` calls, which
    is useful when the profiling boundaries span multiple methods or phases.

    Args:
        output_dir: Directory to write profile artifacts into.
        top_n: Number of functions to include in the summary.
    """

    def __init__(self, output_dir: Path, *, top_n: int = _TOP_N) -> None:
        self._output_dir = output_dir
        self._top_n = top_n
        self._profiler: cProfile.Profile | None = None
        self._start_ts: float = 0.0

    def start(self) -> None:
        """Create a fresh cProfile.Profile and enable it."""
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._profiler = cProfile.Profile()
        self._start_ts = time.monotonic()
        self._profiler.enable()

    def stop(self) -> ProfileResult:
        """Disable the profiler and extract the top functions.

        Returns:
            A ``ProfileResult`` with timing data and top functions.

        Raises:
            RuntimeError: If ``start()`` was not called first.
        """
        if self._profiler is None:
            msg = "OrchestratorProfiler.start() must be called before stop()"
            raise RuntimeError(msg)

        self._profiler.disable()
        elapsed = time.monotonic() - self._start_ts

        top_functions = _extract_top_functions(self._profiler, self._top_n)

        ts = time.strftime("%Y%m%d-%H%M%S")
        prof_path = self._output_dir / f"profile-{ts}.prof"
        txt_path = self._output_dir / f"profile-{ts}.txt"

        # Write binary dump
        self._profiler.dump_stats(str(prof_path))

        # Write human-readable report
        buf = io.StringIO()
        ps = pstats.Stats(self._profiler, stream=buf)
        ps.strip_dirs().sort_stats("cumulative").print_stats(self._top_n)
        report = buf.getvalue()

        header = (
            f"# Bernstein orchestrator profile\n"
            f"# Elapsed: {elapsed:.2f}s\n"
            f"# Top {self._top_n} functions by cumulative time\n\n"
        )
        txt_path.write_text(header + report, encoding="utf-8")

        logger.info("Profile saved to %s", prof_path)
        logger.info("Profile report saved to %s", txt_path)

        return ProfileResult(
            total_time=elapsed,
            top_functions=top_functions,
            output_path=prof_path,
        )

    def save_stats(self, path: Path) -> None:
        """Dump the raw pstats binary to an arbitrary path.

        This is useful for exporting profiles to external tools like
        SnakeViz or py-spy without relying on the default output directory.

        Args:
            path: Destination file path for the ``.prof`` binary.

        Raises:
            RuntimeError: If ``start()`` was not called first.
        """
        if self._profiler is None:
            msg = "OrchestratorProfiler.start() must be called before save_stats()"
            raise RuntimeError(msg)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._profiler.dump_stats(str(path))

    @staticmethod
    def to_markdown(result: ProfileResult) -> str:
        """Render a ``ProfileResult`` as a Markdown table.

        Args:
            result: The profiling result to render.

        Returns:
            A Markdown string with a table of top functions.
        """
        lines: list[str] = [
            "## Orchestrator Profile",
            "",
            f"**Total time:** {result.total_time:.2f}s",
            "",
            "| # | Function | Cumulative (s) | Calls |",
            "|---|----------|---------------:|------:|",
        ]
        for i, (name, cumtime, calls) in enumerate(result.top_functions, 1):
            lines.append(f"| {i} | `{name}` | {cumtime:.4f} | {calls} |")

        if result.output_path is not None:
            lines.append("")
            lines.append(f"Binary profile: `{result.output_path}`")

        return "\n".join(lines) + "\n"


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
        _exc_type: type[BaseException] | None,
        _exc_val: BaseException | None,
        _exc_tb: TracebackType | None,
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


def _extract_top_functions(
    profiler: cProfile.Profile,
    top_n: int,
) -> list[tuple[str, float, int]]:
    """Extract the top N functions by cumulative time from a profiler.

    Args:
        profiler: A disabled cProfile.Profile instance.
        top_n: Maximum number of functions to return.

    Returns:
        List of ``(function_name, cumulative_time, call_count)`` tuples,
        sorted by cumulative time descending.
    """
    stats = pstats.Stats(profiler, stream=io.StringIO())
    stats.sort_stats("cumulative")

    result: list[tuple[str, float, int]] = []
    # pstats.Stats.stats is dict[(file, line, func) -> (cc, nc, tt, ct, callers)]
    # After sort_stats, fcn_list has keys in sorted order.
    for key in stats.fcn_list[:top_n]:  # type: ignore[attr-defined]
        _file, _line, func_name = key
        raw = stats.stats[key]
        # raw is (primitive_calls, total_calls, total_time, cumulative_time, callers)
        cumtime: float = raw[3]
        calls: int = raw[1]
        # Build a readable name: "module.py:42(func)"
        display_name = f"{_file}:{_line}({func_name})"
        result.append((display_name, cumtime, calls))

    return result


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
