"""Reporter entry-point discovery for the pluggy plugin manager."""

from __future__ import annotations

import importlib.metadata as metadata
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "bernstein.reporters"


def discover_reporters(
    register: Callable[[object, str], None],
    *,
    on_failure: Callable[[str, str, str], None] | None = None,
) -> None:
    """Load installed reporter plugins and pass each to *register*.

    Reporters use Bernstein's existing hook specification, so their owning
    runtime surface remains :class:`bernstein.plugins.manager.PluginManager`.

    Args:
        register: Called with ``(reporter, name)`` for each reporter that
            loaded.
        on_failure: Called with ``(name, declared, error)`` for each declared
            reporter that did not. Without it a broken reporter leaves only a
            log line, and the caller cannot tell it apart from one that was
            never declared.
    """
    try:
        reporters = metadata.entry_points(group=ENTRY_POINT_GROUP)
    except Exception as error:
        logger.warning("Failed to enumerate reporter entry points: %s", error)
        return
    for entry_point in reporters:
        try:
            reporter = entry_point.load()
            if isinstance(reporter, type):
                reporter = reporter()
            register(reporter, entry_point.name)
        except Exception as error:
            logger.warning("Failed to load reporter entry-point %r: %s", entry_point.name, error)
            if on_failure is not None:
                on_failure(entry_point.name, entry_point.value, f"{type(error).__name__}: {error}")
