"""Explicit-override-aware attachment of a container sandbox backend.

The deterministic selector (:mod:`bernstein.core.sandbox.selector`) already
treats an explicit ``--sandbox <name>`` override as a hard contract and
raises :class:`~bernstein.core.sandbox.selector.SandboxSelectionError`
rather than silently picking a different runtime. The runtime *attach*
step, however, has to probe a live container runtime, and that probe used
to degrade quietly: a missing Docker SDK or dead daemon logged a warning
and fell through to legacy-container and then plain host-worktree
execution, dropping the isolation boundary the operator explicitly asked
for with no console signal (issue #2809).

This module carries the same override-first contract into the attach step.
When a container runtime is requested by an explicit override, an
unavailable runtime is a loud failure; when it was merely auto-selected,
an unavailable runtime degrades gracefully. Threading the ``explicit`` bit
through is what distinguishes the two, so the loud-fail path is gated on
operator intent, not on runtime availability alone.

The intent test is "did the operator name a container runtime", never
"is the named runtime docker" (issue #3039). :data:`CONTAINER_SANDBOX_RUNTIMES`
is the single source of truth for that question and is derived from the
accepted ``sandbox.runtime`` values, so teaching the configuration about a
new container runtime extends the gate with it instead of leaving the new
runtime failing open.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from typing import TYPE_CHECKING

from bernstein.core.sandbox.selector import SandboxSelectionError
from bernstein.core.security.sandbox import CONTAINER_RUNTIME_NAMES

if TYPE_CHECKING:
    from collections.abc import Callable

    from bernstein.core.sandbox.backends.docker import DockerSandboxBackend

logger = logging.getLogger(__name__)

#: Every container runtime an operator can name with ``--sandbox`` /
#: ``BERNSTEIN_SANDBOX_RUNTIME`` / ``sandbox.runtime``. Aliases the single
#: runtime declaration in :mod:`bernstein.core.security.sandbox` so a newly
#: supported runtime is covered by the explicit-intent gates automatically
#: (issue #3039). Non-container backends (``worktree`` and the cloud
#: backends) are deliberately absent: they have no container boundary to
#: lose and keep their own semantics.
CONTAINER_SANDBOX_RUNTIMES: frozenset[str] = CONTAINER_RUNTIME_NAMES

#: Seconds allowed for the ``<runtime> info`` liveness probe.
_RUNTIME_PROBE_TIMEOUT_S = 10


def is_container_runtime(runtime: str | None) -> bool:
    """Whether *runtime* names a container runtime the operator can pin.

    Args:
        runtime: Raw runtime name, e.g. from ``BERNSTEIN_SANDBOX_RUNTIME``.
            Case and surrounding whitespace are ignored; ``None`` and the
            empty string mean "no runtime was named".

    Returns:
        ``True`` when *runtime* is one of :data:`CONTAINER_SANDBOX_RUNTIMES`.
    """
    return (runtime or "").strip().lower() in CONTAINER_SANDBOX_RUNTIMES


def probe_runtime_cli(runtime: str) -> str | None:
    """Probe a container runtime CLI for presence and responsiveness.

    Mirrors what :class:`~bernstein.core.container.ContainerManager` does at
    spawn time (resolve the CLI on ``PATH``, then ask it for ``info``) so an
    unusable runtime surfaces at wiring time instead of per spawn.

    Args:
        runtime: CLI name to probe, e.g. ``"podman"``.

    Returns:
        ``None`` when the runtime is usable, otherwise a human-readable
        description of why it is not.
    """
    if shutil.which(runtime) is None:
        return f"Container runtime CLI '{runtime}' not found on PATH."
    try:
        # Fixed argv; ``runtime`` comes from the closed CONTAINER_SANDBOX_RUNTIMES set.
        result = subprocess.run(
            [runtime, "info"],
            capture_output=True,
            timeout=_RUNTIME_PROBE_TIMEOUT_S,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return f"'{runtime} info' did not complete: {exc}."
    if result.returncode != 0:
        stderr_lines = result.stderr.decode("utf-8", errors="replace").strip().splitlines()
        detail = stderr_lines[-1] if stderr_lines else f"exit status {result.returncode}"
        return f"'{runtime} info' failed: {detail}."
    return None


def attach_container_backend(
    runtime: str,
    *,
    explicit: bool,
    backend: DockerSandboxBackend | None = None,
    probe: Callable[[str], str | None] | None = None,
) -> DockerSandboxBackend | None:
    """Verify the named container runtime and attach a backend when it has one.

    Issue #3039: the wiring-time availability gate used to run only for
    ``docker``, so ``--sandbox podman`` skipped the probe entirely and an
    absent podman fell through to worktree execution with no refusal. The
    gate now keys on "the operator named a container runtime", so every
    accepted runtime fails closed on an explicit request.

    Runtimes that ship a first-party :class:`SandboxBackend` (currently
    ``docker``) return that backend so the caller can provision one session
    per agent. Runtimes served by the CLI-driven sandbox path (currently
    ``podman``) return ``None`` after a successful probe: the caller keeps
    its existing execution route, and the probe's only job is to make an
    unavailable runtime refuse rather than degrade.

    Args:
        runtime: Runtime the operator named (``--sandbox <runtime>``).
            Non-container names return ``None`` unprobed.
        explicit: Whether *runtime* came from an explicit operator override
            rather than automatic selection. Only the explicit path fails
            loudly; auto-selection keeps the historical graceful fallback.
        backend: Pre-built Docker backend to verify, forwarded to
            :func:`attach_docker_backend`. Tests inject a stub.
        probe: Override for :func:`probe_runtime_cli`, used by tests to
            exercise the unavailable-runtime branch without a real CLI.

    Returns:
        A ready backend when the runtime has one and is usable, otherwise
        ``None``. ``None`` never means "isolation was dropped": on the
        explicit path an unusable runtime raises instead.

    Raises:
        SandboxSelectionError: When ``explicit`` is ``True`` and the named
            container runtime is unavailable. The operator asked for a
            container boundary; degrading to host or worktree execution
            would drop it without a signal.
    """
    normalized = (runtime or "").strip().lower()
    if normalized not in CONTAINER_SANDBOX_RUNTIMES:
        # worktree and the cloud backends are not container runtimes: they
        # have no CLI to probe and their own selection path already reports
        # an explicit override that cannot be satisfied.
        return None

    if normalized == "docker":
        return attach_docker_backend(explicit=explicit, backend=backend)

    failure = (probe or probe_runtime_cli)(normalized)
    if failure is not None:
        if explicit:
            raise SandboxSelectionError(
                f"Explicit '--sandbox {normalized}' could not be honored: "
                f"{failure} Refusing to fall back to host execution because "
                "container isolation was explicitly requested. Re-run "
                "without --sandbox to allow automatic fallback, or install "
                f"{normalized} and make sure it is running.",
                attempted=(normalized,),
            )
        logger.warning(
            "Auto-selected sandbox runtime %s is unavailable, falling back: %s",
            normalized,
            failure,
        )
        return None

    logger.info("Container runtime %s verified at wiring time", normalized)
    return None


def attach_docker_backend(
    *,
    explicit: bool,
    backend: DockerSandboxBackend | None = None,
) -> DockerSandboxBackend | None:
    """Instantiate and verify the Docker sandbox backend.

    The backend's availability probe (SDK import + daemon ping) runs at
    wiring time so a broken setup surfaces before any agent spawns rather
    than per spawn.

    Args:
        explicit: Whether Docker was chosen by an explicit operator
            override (``--sandbox docker``) as opposed to automatic
            selection. Only the explicit path fails loudly; auto-selection
            keeps the historical graceful fallback.
        backend: Pre-built backend to verify. Defaults to a fresh
            :class:`~bernstein.core.sandbox.backends.docker.DockerSandboxBackend`.
            Tests inject a stub to exercise the availability branches
            without a live daemon.

    Returns:
        The ready backend when Docker is usable. ``None`` when Docker is
        unavailable *and* the choice was auto-selected (``explicit`` is
        ``False``), signalling the caller to fall back gracefully.

    Raises:
        SandboxSelectionError: When ``explicit`` is ``True`` and Docker is
            unavailable. The operator requested container isolation with an
            explicit ``--sandbox docker`` override; silently degrading to
            host execution would drop that isolation boundary without a
            signal, so the failure is raised instead of swallowed.
    """
    from bernstein.core.sandbox.backends.docker import (
        DockerSandboxBackend,
        DockerUnavailableError,
    )

    candidate = backend if backend is not None else DockerSandboxBackend()
    try:
        candidate.ensure_available()
    except DockerUnavailableError as exc:
        if explicit:
            raise SandboxSelectionError(
                "Explicit '--sandbox docker' could not be honored: "
                f"{exc} Refusing to fall back to host execution because "
                "container isolation was explicitly requested. Re-run "
                "without --sandbox to allow automatic fallback, or install "
                "the Docker SDK and start the Docker daemon.",
                attempted=("docker",),
            ) from exc
        return None
    return candidate


__all__ = [
    "CONTAINER_SANDBOX_RUNTIMES",
    "attach_container_backend",
    "attach_docker_backend",
    "is_container_runtime",
    "probe_runtime_cli",
]
