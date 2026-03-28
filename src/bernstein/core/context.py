"""Gather project context for the manager's planning prompt.

This is a facade module that re-exports from specialized submodules:
- file_discovery: File tree and project context gathering
- knowledge_base: Knowledge base, file indexing, task context
- api_usage: API usage tracking and metrics

Backward compatibility: all imports from the original context module
continue to work through re-exports.
"""

from __future__ import annotations

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
    FileSummary,
    FileIndexEntry,
    TaskContextBuilder,
    append_decision,
    build_architecture_md,
    build_file_index,
    refresh_knowledge_base,
)

# Re-export API usage tracking
from bernstein.core.api_usage import (
    ApiCallRecord,
    ApiUsageTracker,
    AgentSessionUsage,
    ProviderUsageSummary,
    TierConsumption,
    get_usage_tracker,
)

__all__ = [
    # File discovery
    "available_roles",
    "clear_caches",
    "file_tree",
    "gather_project_context",
    "gather_project_memory",
    "get_recent_project_memory",
    # Knowledge base
    "FileSummary",
    "FileIndexEntry",
    "TaskContextBuilder",
    "append_decision",
    "build_architecture_md",
    "build_file_index",
    "refresh_knowledge_base",
    # API usage
    "ApiCallRecord",
    "ApiUsageTracker",
    "AgentSessionUsage",
    "ProviderUsageSummary",
    "TierConsumption",
    "get_usage_tracker",
]
