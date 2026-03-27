"""Gather project context for the manager's planning prompt.

This is a facade module that re-exports from specialized submodules:
- file_discovery: File tree and project context gathering
- knowledge_base: Knowledge base, file indexing, task context
- api_usage: API usage tracking and metrics

Backward compatibility: all imports from the original context module
continue to work through re-exports.
"""

from __future__ import annotations

# Re-export API usage tracking
from bernstein.core.api_usage import (
    AgentSessionUsage,
    ApiCallRecord,
    ApiUsageTracker,
    ProviderUsageSummary,
    TierConsumption,
    get_usage_tracker,
)

# Re-export file discovery functions
from bernstein.core.file_discovery import (
    available_roles,
    clear_caches,
    file_tree,
    gather_project_context,
    gather_project_memory,
    get_recent_project_memory,
)

# Re-export knowledge base classes and functions
from bernstein.core.knowledge_base import (
    FileIndexEntry,
    FileSummary,
    TaskContextBuilder,
    append_decision,
    build_architecture_md,
    build_file_index,
    refresh_knowledge_base,
)

__all__ = [
    "AgentSessionUsage",
    # API usage
    "ApiCallRecord",
    "ApiUsageTracker",
    "FileIndexEntry",
    # Knowledge base
    "FileSummary",
    "ProviderUsageSummary",
    "TaskContextBuilder",
    "TierConsumption",
    "append_decision",
    # File discovery
    "available_roles",
    "build_architecture_md",
    "build_file_index",
    "clear_caches",
    "file_tree",
    "gather_project_context",
    "gather_project_memory",
    "get_recent_project_memory",
    "get_usage_tracker",
    "refresh_knowledge_base",
]
