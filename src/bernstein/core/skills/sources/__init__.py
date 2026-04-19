"""Concrete :class:`~bernstein.core.skills.source.SkillSource` implementations."""

from __future__ import annotations

from bernstein.core.skills.sources.local_dir import LocalDirSkillSource
from bernstein.core.skills.sources.plugin import (
    PLUGIN_ENTRY_POINT_GROUP,
    PluginSkillSource,
    load_plugin_sources,
)

__all__ = [
    "PLUGIN_ENTRY_POINT_GROUP",
    "LocalDirSkillSource",
    "PluginSkillSource",
    "load_plugin_sources",
]
