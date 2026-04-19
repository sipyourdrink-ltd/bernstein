"""Pluggable sandbox backends for agent isolation (oai-002 phase 1).

Bernstein's agent isolation has historically been git-worktree-only.
This package introduces a protocol-based abstraction so isolation is
no longer worktree-hardcoded — new backends (Docker, E2B, Modal ...)
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
    FileEntry,
    GitRepoEntry,
    WorkspaceManifest,
)
from bernstein.core.sandbox.registry import (
    get_backend,
    list_backend_names,
    list_backends,
    register_backend,
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
    "DockerSandbox",
    "ExecResult",
    "FileEntry",
    "GitRepoEntry",
    "SandboxBackend",
    "SandboxCapability",
    "SandboxRuntime",
    "SandboxSession",
    "WorkspaceManifest",
    "get_backend",
    "list_backend_names",
    "list_backends",
    "parse_docker_sandbox",
    "register_backend",
    "spawn_in_sandbox",
]
