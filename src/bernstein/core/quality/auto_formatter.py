"""Language-aware auto-formatter with multi-language registry.

Provides a registry of formatter configurations and a function to format
changed files using the appropriate language formatter.  Formatters that
are not installed are silently skipped (``FileNotFoundError`` is caught).
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FormatterConfig:
    """Configuration for a single language formatter.

    Attributes:
        language: Human-readable language name (e.g. ``"Python"``).
        command: Command tokens to invoke the formatter.  Files are appended
            as extra positional arguments.
        extensions: File extensions that this formatter handles (including
            the leading dot, e.g. ``".py"``).
        timeout_s: Per-invocation timeout in seconds.
    """

    language: str
    command: tuple[str, ...]
    extensions: frozenset[str]
    timeout_s: int = 60


@dataclass(frozen=True)
class FormatResult:
    """Outcome of formatting a batch of files.

    Attributes:
        files_formatted: Number of files that were reformatted.
        files_unchanged: Number of files that were already well-formatted.
        formatter_used: Language label of the formatter that ran.
        duration_s: Wall-clock seconds the formatter took.
        error: Human-readable error message, or ``None`` on success.
    """

    files_formatted: int
    files_unchanged: int
    formatter_used: str
    duration_s: float
    error: str | None = field(default=None)


# ---------------------------------------------------------------------------
# Default registry
# ---------------------------------------------------------------------------

_DEFAULT_REGISTRY: tuple[FormatterConfig, ...] = (
    FormatterConfig(
        language="Python",
        command=("ruff", "format"),
        extensions=frozenset({".py"}),
    ),
    FormatterConfig(
        language="JS/TS",
        command=("prettier", "--write"),
        extensions=frozenset({".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}),
    ),
    FormatterConfig(
        language="Rust",
        command=("rustfmt",),
        extensions=frozenset({".rs"}),
    ),
    FormatterConfig(
        language="Go",
        command=("gofmt", "-w"),
        extensions=frozenset({".go"}),
    ),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _group_files_by_formatter(
    files: Sequence[str],
    registry: Sequence[FormatterConfig],
) -> dict[FormatterConfig, list[str]]:
    """Group *files* by the first matching formatter in *registry*.

    Files whose extension does not match any formatter are silently ignored.

    Args:
        files: Iterable of file paths (relative or absolute).
        registry: Ordered sequence of formatter configs.

    Returns:
        Mapping from ``FormatterConfig`` to the list of matching file paths.
    """
    groups: dict[FormatterConfig, list[str]] = {}
    for fpath in files:
        suffix = Path(fpath).suffix
        for cfg in registry:
            if suffix in cfg.extensions:
                groups.setdefault(cfg, []).append(fpath)
                break  # first match wins
    return groups


def _run_single_formatter(
    cfg: FormatterConfig,
    files: list[str],
    workdir: Path,
    timeout_s: int,
) -> FormatResult:
    """Invoke one formatter on *files* and return its ``FormatResult``.

    Args:
        cfg: Formatter configuration.
        files: List of file paths to format.
        workdir: Working directory for the subprocess.
        timeout_s: Override timeout (capped by per-config ``cfg.timeout_s``).

    Returns:
        A ``FormatResult`` describing what happened.
    """
    effective_timeout = min(timeout_s, cfg.timeout_s) if timeout_s else cfg.timeout_s
    cmd = list(cfg.command) + files
    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            cwd=workdir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=effective_timeout,
        )
    except FileNotFoundError:
        elapsed = time.monotonic() - start
        logger.info(
            "auto_format: %s formatter not installed (%s)",
            cfg.language,
            cfg.command[0],
        )
        return FormatResult(
            files_formatted=0,
            files_unchanged=len(files),
            formatter_used=cfg.language,
            duration_s=elapsed,
            error=f"{cfg.command[0]!r} not found",
        )
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - start
        logger.warning(
            "auto_format: %s formatter timed out after %ds",
            cfg.language,
            effective_timeout,
        )
        return FormatResult(
            files_formatted=0,
            files_unchanged=len(files),
            formatter_used=cfg.language,
            duration_s=elapsed,
            error=f"timed out after {effective_timeout}s",
        )
    except OSError as exc:
        elapsed = time.monotonic() - start
        logger.warning("auto_format: %s formatter OS error: %s", cfg.language, exc)
        return FormatResult(
            files_formatted=0,
            files_unchanged=len(files),
            formatter_used=cfg.language,
            duration_s=elapsed,
            error=str(exc),
        )

    elapsed = time.monotonic() - start

    # Return codes: 0 = ok, 1 = files changed (ruff convention).
    # Anything else is an error but we still don't block.
    if proc.returncode not in (0, 1):
        snippet = (proc.stderr or proc.stdout)[:200]
        logger.warning(
            "auto_format: %s formatter exited %d: %s",
            cfg.language,
            proc.returncode,
            snippet,
        )
        return FormatResult(
            files_formatted=0,
            files_unchanged=len(files),
            formatter_used=cfg.language,
            duration_s=elapsed,
            error=f"exit code {proc.returncode}",
        )

    # Heuristic: count "reformatted" mentions in stdout (ruff convention).
    output = (proc.stdout or "").strip()
    reformatted = 0
    if "reformatted" in output:
        for token in output.split():
            try:
                reformatted = int(token)
                break
            except ValueError:
                continue
    elif proc.returncode == 1:
        # Some formatters signal "files changed" via exit code 1.
        reformatted = len(files)

    return FormatResult(
        files_formatted=reformatted,
        files_unchanged=len(files) - reformatted,
        formatter_used=cfg.language,
        duration_s=elapsed,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def auto_format_changed_files(
    workdir: Path,
    changed_files: Sequence[str],
    registry: Sequence[FormatterConfig] | None = None,
    timeout_s: int = 120,
) -> list[FormatResult]:
    """Format *changed_files* using the appropriate language formatter.

    Each file is matched against *registry* by extension.  The first matching
    formatter wins.  Formatters that are not installed (``FileNotFoundError``)
    are handled gracefully and reported in the result.

    Args:
        workdir: Working directory for subprocess invocations.
        changed_files: List of changed file paths (relative to *workdir*).
        registry: Ordered formatter configs.  Defaults to
            ``_DEFAULT_REGISTRY``.
        timeout_s: Global timeout budget for all formatters (each invocation
            is individually capped).

    Returns:
        A list of ``FormatResult`` objects, one per formatter that was
        applicable to at least one file.
    """
    if registry is None:
        registry = _DEFAULT_REGISTRY

    if not changed_files:
        return []

    groups = _group_files_by_formatter(changed_files, registry)
    if not groups:
        return []

    results: list[FormatResult] = []
    for cfg, files in groups.items():
        result = _run_single_formatter(cfg, files, workdir, timeout_s)
        results.append(result)

    return results
