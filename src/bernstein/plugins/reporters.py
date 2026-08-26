"""Reporter entry-point discovery for the pluggy plugin manager."""

from __future__ import annotations

import importlib.metadata as metadata
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "bernstein.reporters"


def discover_reporters(register: Callable[[object, str], None]) -> None:
    """Load installed reporter plugins and pass each to *register*.

    Reporters use Bernstein's existing hook specification, so their owning
    runtime surface remains :class:`bernstein.plugins.manager.PluginManager`.
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
