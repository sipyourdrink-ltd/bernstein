"""Bounded execution for registered deep agent detectors."""

from __future__ import annotations

import queue
import random
import threading
import time
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Callable

# The richest built-ins (OpenCode and Kiro) may run three sequential subprocess
# probes, each with the discovery module's existing 3s per-probe allowance.
# Keep the outer coordinator deadline finite while preserving that cumulative
# budget plus a small amount of Python/thread scheduling headroom.
_DETECTOR_TIMEOUT_S = 10.0
_DETECTOR_START_JITTER_S = 0.05


def run_registered_detector[T](
    adapter: Callable[[str], T],
    name: str,
    *,
    source: str,
) -> T:
    """Run one detector with full-jitter start delay and a hard deadline.

    The adapter runs in a daemon worker. A timeout stops the discovery
    coordinator from waiting any longer; a result that arrives later is ignored.

    Args:
        adapter: Registered detector callable.
        name: Registry entity name supplied to the detector.
        source: Registration source included in bounded error messages.

    Returns:
        The adapter result.

    Raises:
        TimeoutError: The detector exceeded the coordinator deadline.
        Exception: The detector raised; the original exception is propagated.
        RuntimeError: The worker exited without publishing a result.
    """
    time.sleep(random.random() * _DETECTOR_START_JITTER_S)
    result_queue: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)

    def _worker() -> None:
        try:
            result = adapter(name)
        except Exception as exc:
            result_queue.put((False, exc))
        else:
            result_queue.put((True, result))

    worker = threading.Thread(target=_worker, name=f"bernstein-detector-{name}", daemon=True)
    worker.start()
    worker.join(_DETECTOR_TIMEOUT_S)
    if worker.is_alive():
        raise TimeoutError(f"agent detector from {source} timed out after {_DETECTOR_TIMEOUT_S:.1f}s for {name}")

    try:
        answered, payload = result_queue.get_nowait()
    except queue.Empty as exc:
        raise RuntimeError(f"agent detector from {source} returned no result") from exc
    if not answered:
        if isinstance(payload, Exception):
            raise payload
        raise RuntimeError(f"agent detector from {source} failed")
    return cast("T", payload)
