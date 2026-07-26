"""Lifecycle wire-in for the blast-radius reversibility gate (issue #1322).

This module is the single integration point between the blast-radius scorer
in :mod:`bernstein.core.quality.blast_radius` and the merge / deploy gate.
:func:`install_blast_radius_gate` registers the hook on a
:class:`bernstein.core.security.blocking_hooks.BlockingHookRunner`;
:func:`evaluate_pre_merge` is the caller-facing entry point that builds a
runner, installs the hook and evaluates one candidate merge.  Both are
no-ops unless the operator opted in via ``--max-blast-radius`` (which
propagates the ``BERNSTEIN_MAX_BLAST_RADIUS`` env var).

Default behaviour stays unchanged: when the env var is unset, no hook is
registered and the gate is a pass-through.

Requesting a ceiling that cannot be honoured is not the same as not
requesting one.  When the env var carries a value the gate cannot use,
:func:`evaluate_pre_merge` denies instead of passing, so a merge never
reports success under a ceiling that was never evaluated.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from bernstein.core.security.blocking_hooks import BlockingHookResult, BlockingHookRunner

logger = logging.getLogger(__name__)

#: Operator-visible env var that carries the ceiling from `bernstein run`.
ENV_MAX_BLAST_RADIUS: str = "BERNSTEIN_MAX_BLAST_RADIUS"

#: Blocking-hook event this gate registers on.
PRE_MERGE_EVENT: str = "pre_merge"


def ceiling_requested() -> bool:
    """Return ``True`` when the operator asked for a ceiling at all.

    Distinct from :func:`_read_ceiling` returning a value: a malformed or
    out-of-range setting is still a request, and must not be treated as
    "no gate wanted".
    """
    raw = os.environ.get(ENV_MAX_BLAST_RADIUS)
    return raw is not None and raw.strip() != ""


def _read_ceiling() -> float | None:
    """Parse the env var and clamp to [0, 1]. Returns ``None`` when unset."""
    raw = os.environ.get(ENV_MAX_BLAST_RADIUS)
    if raw is None or raw.strip() == "":
        return None
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Ignoring %s=%r: not a valid float in [0, 1].", ENV_MAX_BLAST_RADIUS, raw)
        return None
    if not 0.0 <= value <= 1.0:
        logger.warning("Ignoring %s=%s: outside [0, 1].", ENV_MAX_BLAST_RADIUS, value)
        return None
    return value


def install_blast_radius_gate(runner: BlockingHookRunner) -> bool:
    """Register the blast-radius hook on ``runner`` if the env var is set.

    Args:
        runner: Existing :class:`BlockingHookRunner`.

    Returns:
        ``True`` when a hook was registered, ``False`` when the gate was
        skipped (env var unset or invalid).
    """
    ceiling = _read_ceiling()
    if ceiling is None:
        return False
    # Late import to avoid pulling YAML / scorer code into modules that
    # never opt in to the gate.
    from bernstein.core.quality.blast_radius import make_pre_merge_hook

    hook = make_pre_merge_hook(max_score=ceiling)
    runner.register(PRE_MERGE_EVENT, hook)
    logger.info("Blast-radius reversibility gate active: ceiling=%.4f (pre_merge).", ceiling)
    return True


def evaluate_pre_merge(*, files: Sequence[str], diff_text: str) -> BlockingHookResult | None:
    """Evaluate one candidate merge against the operator's ceiling.

    Builds a :class:`BlockingHookRunner`, installs the gate on it through
    :func:`install_blast_radius_gate`, and runs the ``pre_merge`` event for
    the supplied change.

    Args:
        files: Paths the merge would bring in.
        diff_text: Diff body for the content detectors.

    Returns:
        ``None`` when no ceiling was requested, so the caller proceeds
        exactly as before.  Otherwise the hook result: ``allowed=False``
        carries a reason naming the computed score and the ceiling.
    """
    if not ceiling_requested():
        return None

    from bernstein.core.hook_events import BlockingHookPayload, HookEvent
    from bernstein.core.security.blocking_hooks import BlockingHookResult, BlockingHookRunner

    raw = os.environ.get(ENV_MAX_BLAST_RADIUS, "")
    runner = BlockingHookRunner()
    try:
        if not install_blast_radius_gate(runner):
            # The operator asked for a ceiling and did not get one.  Passing
            # here would report a gate that was never evaluated.
            return BlockingHookResult(
                allowed=False,
                reason=(
                    f"refused: {ENV_MAX_BLAST_RADIUS}={raw!r} is not a usable ceiling "
                    f"in [0, 1], so the requested blast-radius gate could not be installed"
                ),
                hook_name="blast_radius",
            )
        payload = BlockingHookPayload(
            event=HookEvent.PRE_MERGE,
            action="merge",
            context={"files": tuple(files), "diff_text": diff_text},
        )
        return runner.run(PRE_MERGE_EVENT, payload)
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("Blast-radius gate could not be evaluated: %s", exc)
        return BlockingHookResult(
            allowed=False,
            reason=f"refused: blast-radius gate could not be evaluated ({exc})",
            hook_name="blast_radius",
        )
    finally:
        runner.shutdown()


__all__ = [
    "ENV_MAX_BLAST_RADIUS",
    "PRE_MERGE_EVENT",
    "ceiling_requested",
    "evaluate_pre_merge",
    "install_blast_radius_gate",
]
