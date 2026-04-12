"""Backward-compat shim — re-exports from bernstein.core.observability.postmortem."""

from bernstein.core.observability.postmortem import (
    ContributingFactor,
    FailedTaskTrace,
    PostMortemEvent,
    PostMortemGenerator,
    PostMortemReport,
    RecommendedAction,
    logger,
)

__all__ = [
    "ContributingFactor",
    "FailedTaskTrace",
    "PostMortemEvent",
    "PostMortemGenerator",
    "PostMortemReport",
    "RecommendedAction",
    "logger",
]
