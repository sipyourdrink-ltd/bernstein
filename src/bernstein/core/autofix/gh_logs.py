"""Failing-log extraction via the ``gh`` CLI.

The autofix daemon needs the *failed* portion of a CI run to feed
into the classifier and the synthesised goal.  GitHub's CLI exposes
this directly with ``gh run view <run-id> --log-failed``; this
module wraps the call, applies a configurable byte budget so the
classifier never sees a multi-megabyte log, and returns a typed
result so downstream code never inspects raw subprocess state.

The wrapper is intentionally tolerant of three failure modes:

1. ``gh`` is not on ``$PATH``.  The dispatcher should fail open (skip
   the attempt with a clear reason) rather than crashing the daemon.
2. ``gh`` exits non-zero (auth issue, run id not found).
3. The log is *empty* — sometimes a job fails before producing
   output.  In that case the classifier still runs against the empty
   string and falls back to the default bucket.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from typing import Protocol


class _Runner(Protocol):
    """Minimal protocol around :func:`subprocess.run` used in tests."""

    def __call__(
        self,
        cmd: list[str],
        *,
        capture_output: bool,
        text: bool,
        timeout: float,
        check: bool,
    ) -> subprocess.CompletedProcess[str]: ...


@dataclass(frozen=True)
class LogExtraction:
    """A failing-log fetch result.

    Attributes:
        ok: ``True`` when the extraction succeeded, even if the log
            body is empty.  ``False`` when ``gh`` was missing or
            errored out.
        body: The captured log text, head-truncated to the byte
            budget supplied by the caller.
        truncated: ``True`` when the original log was longer than the
            byte budget and head-truncation was applied.
        error: Human-readable failure reason; empty on success.
    """

    ok: bool
    body: str
    truncated: bool = False
    error: str = ""


def _truncate(text: str, byte_budget: int) -> tuple[str, bool]:
    """Head-truncate ``text`` to ``byte_budget`` bytes.

    UTF-8 bytes are accounted for so the truncation cannot leave a
    partial code-point at the boundary.

    Args:
        text: The log body.
        byte_budget: Maximum encoded length to return; non-positive
            values disable truncation.

    Returns:
        ``(truncated_text, did_truncate)``.
    """
    if byte_budget <= 0:
        return text, False

    encoded = text.encode("utf-8")
    if len(encoded) <= byte_budget:
        return text, False

    # Drop trailing bytes until decoding succeeds (handles a UTF-8
    # split across the budget boundary).
    cut = encoded[:byte_budget]
    while cut:
        try:
            return cut.decode("utf-8"), True
        except UnicodeDecodeError:
            cut = cut[:-1]
    return "", True


def extract_failed_log(
    run_id: str,
    *,
    byte_budget: int,
    repo: str | None = None,
    runner: _Runner | None = None,
    timeout_seconds: float = 60.0,
) -> LogExtraction:
    """Fetch the failing portion of a GitHub Actions run.

    Args:
        run_id: GitHub Actions run identifier (numeric or string).
        byte_budget: Hard cap on the returned body length in bytes.
        repo: Optional ``owner/name`` repo override; passed to ``gh``
            via ``-R`` so the CLI does not require the operator to
            ``cd`` into the right checkout.
        runner: Test seam — when provided this callable is used
            instead of :func:`subprocess.run`.  The default behaviour
            calls ``gh`` directly.
        timeout_seconds: Subprocess timeout.  ``gh`` typically
            returns within a few seconds; the default leaves headroom
            for slow links.

    Returns:
        A :class:`LogExtraction` describing the outcome.  Callers
        should always inspect ``ok`` before using ``body``.
    """
    if shutil.which("gh") is None and runner is None:
        return LogExtraction(
            ok=False,
            body="",
            truncated=False,
            error="`gh` CLI not found on PATH",
        )

    cmd: list[str] = ["gh", "run", "view", str(run_id), "--log-failed"]
    if repo:
        cmd.extend(["-R", repo])

    use_runner: _Runner = runner if runner is not None else subprocess.run  # type: ignore[assignment]

    try:
        result = use_runner(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return LogExtraction(
            ok=False,
            body="",
            truncated=False,
            error=f"`gh run view` timed out after {timeout_seconds:.0f}s",
        )
    except OSError as exc:
        return LogExtraction(
            ok=False,
            body="",
            truncated=False,
            error=f"failed to invoke gh: {exc}",
        )

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        return LogExtraction(
            ok=False,
            body="",
            truncated=False,
            error=stderr or f"gh exited with code {result.returncode}",
        )

    body, truncated = _truncate(result.stdout or "", byte_budget)
    return LogExtraction(ok=True, body=body, truncated=truncated, error="")
