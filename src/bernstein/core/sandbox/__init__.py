"""Pluggable sandbox backends for agent isolation (oai-002 phase 1).

Bernstein's agent isolation has historically been git-worktree-only.
This package introduces a protocol-based abstraction so isolation is
no longer worktree-hardcoded - new backends (Docker, E2B, Modal ...)
can register via the ``bernstein.sandbox_backends`` entry-point group.

Phase 1 exposes the protocol, manifest, registry, and four first-party
backends (``worktree``, ``docker`` in core; ``e2b``, ``modal`` via
optional extras). The spawner is modified only to accept an OPTIONAL
``sandbox_session`` parameter; when ``None`` the existing direct-
worktree path is used so no adapter behaviour changes.

Public API::

    from bernstein.core.sandbox import (
        SandboxBackend,
        SandboxCapability,
        SandboxSession,
        WorkspaceManifest,
        GitRepoEntry,
        FileEntry,
        ExecResult,
        get_backend,
        list_backends,
        list_backend_names,
        register_backend,
    )
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Back-compat re-exports.
#
# Prior to oai-002 the path ``bernstein.core.sandbox`` was resolved by the
# ``_CoreRedirectFinder`` in ``bernstein.core`` to ``bernstein.core.security.sandbox``.
# That redirect is shadowed now that ``sandbox`` is a real package, so we
# re-export the legacy Docker-container primitives under the same names to
# keep existing callers (``spawner_core.py``, ``seed_parser.py`` etc.)
# working unchanged. The new protocol-based primitives are namespaced
# separately and do not collide.
# ---------------------------------------------------------------------------
from typing import Any as _Any

from bernstein.core.sandbox.backend import (
    ExecResult,
    SandboxBackend,
    SandboxCapability,
    SandboxSession,
)
from bernstein.core.sandbox.manifest import (
    ArtifactMount,
    AzureBlobMount,
    FileEntry,
    GCSMount,
    GitRepoEntry,
    R2Mount,
    S3Mount,
    WorkspaceManifest,
)
from bernstein.core.sandbox.pool import (
    PoolManifest,
    PoolMergeResult,
    PoolOverrideRefused,
    PoolWorkspaceTemplate,
    merge_pool_overrides,
)
from bernstein.core.sandbox.pool_registry import (
    PoolRegistry,
    PoolStore,
    project_pool_registry,
)
from bernstein.core.sandbox.registry import (
    get_backend,
    list_backend_names,
    list_backends,
    register_backend,
)
from bernstein.core.sandbox.selector import (
    DEFAULT_PRECEDENCE,
    FREE_BACKENDS,
    SandboxEnvironment,
    SandboxPolicy,
    SandboxSelectionError,
    select_sandbox,
)
from bernstein.core.security.sandbox import (
    DockerSandbox,
    SandboxRuntime,
    parse_docker_sandbox,
)
from bernstein.core.security.sandbox import (
    spawn_in_sandbox as _spawn_in_sandbox,  # pyright: ignore[reportUnknownVariableType]
)

# Pyright flags ``spawn_in_sandbox`` as partially unknown because the
# legacy module's signature uses one inferred parameter. The public
# re-export is typed as ``Any`` to surface a clean back-compat API
# until the legacy module gets strict typing in a later ticket.
spawn_in_sandbox: _Any = _spawn_in_sandbox  # pyright: ignore[reportUnknownVariableType]

__all__ = [
    "DEFAULT_PRECEDENCE",
    "FREE_BACKENDS",
    "ArtifactMount",
    "AzureBlobMount",
    "DockerSandbox",
    "ExecResult",
    "FileEntry",
    "GCSMount",
    "GitRepoEntry",
    "PoolManifest",
    "PoolMergeResult",
    "PoolOverrideRefused",
    "PoolRegistry",
    "PoolStore",
    "PoolWorkspaceTemplate",
    "R2Mount",
    "S3Mount",
    "SandboxBackend",
    "SandboxCapability",
    "SandboxEnvironment",
    "SandboxPolicy",
    "SandboxRuntime",
    "SandboxSelectionError",
    "SandboxSession",
    "WorkspaceManifest",
    "get_backend",
    "list_backend_names",
    "list_backends",
    "merge_pool_overrides",
    "parse_docker_sandbox",
    "project_pool_registry",
    "register_backend",
    "select_sandbox",
    "spawn_in_sandbox",
]
