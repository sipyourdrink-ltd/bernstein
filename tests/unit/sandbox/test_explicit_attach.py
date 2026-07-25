"""Unit tests for explicit-override-aware container backend attachment.

Regression coverage for issue #2809: an explicit ``--sandbox docker``
override that cannot be satisfied (missing Docker SDK, dead daemon) must
fail loudly with :class:`SandboxSelectionError` instead of silently
degrading to host / worktree execution. Auto-selected Docker (no explicit
override) must still fall back quietly.

Regression coverage for issue #3039: the wiring-time gate keys on "the
operator named a container runtime", never on the literal ``"docker"``. A
docker-only gate let ``--sandbox podman`` skip the probe entirely, so the
runtime that failed closed was the one the gate happened to name and every
other one failed open. The parametrised cases below run over the canonical
runtime set, so adding a runtime cannot reintroduce the gap silently.
"""

from __future__ import annotations

from typing import Any

import pytest

from bernstein.core.sandbox.backends.docker import DockerUnavailableError
from bernstein.core.sandbox.explicit_attach import (
    CONTAINER_SANDBOX_RUNTIMES,
    attach_container_backend,
    attach_docker_backend,
    is_container_runtime,
)
from bernstein.core.sandbox.selector import SandboxSelectionError

# ``--sandbox`` choices that are not container runtimes. They own their own
# provisioning semantics and must not be probed or refused by this gate.
NON_CONTAINER_SANDBOX_CHOICES = (
    "worktree",
    "e2b",
    "modal",
    "daytona",
    "blaxel",
    "runloop",
    "vercel",
    "microvm",
)


def _unavailable(runtime: str) -> str:
    """Probe stub standing in for a runtime CLI that is not installed."""
    return f"Container runtime CLI '{runtime}' not found on PATH."


def _available(runtime: str) -> None:
    """Probe stub standing in for a healthy runtime CLI."""
    del runtime
    return None


class _UnavailableBackend:
    """Docker backend stub whose availability probe always fails."""

    name = "docker"

    def ensure_available(self) -> None:
        raise DockerUnavailableError(
            "Install the 'docker' extra to use DockerSandboxBackend: `pip install bernstein[docker]`."
        )


class _AvailableBackend:
    """Docker backend stub whose availability probe succeeds."""

    name = "docker"

    def ensure_available(self) -> None:
        return None


def test_explicit_docker_unavailable_raises_selection_error() -> None:
    """Explicit override + unavailable Docker must raise, not fall back."""
    with pytest.raises(SandboxSelectionError) as excinfo:
        attach_docker_backend(explicit=True, backend=_UnavailableBackend())  # type: ignore[arg-type]

    err = excinfo.value
    assert err.attempted == ("docker",)
    # The message must name the actual cause so the operator can fix it.
    assert "docker" in err.reason.lower()
    assert "host" in err.reason.lower()


def test_explicit_docker_unavailable_does_not_return_host_backend() -> None:
    """The explicit failure path must never yield a usable backend."""
    with pytest.raises(SandboxSelectionError):
        _result: Any = attach_docker_backend(
            explicit=True,
            backend=_UnavailableBackend(),  # type: ignore[arg-type]
        )


def test_auto_selected_docker_unavailable_falls_back_quietly() -> None:
    """Auto-selection (explicit False) degrades gracefully to ``None``."""
    result = attach_docker_backend(explicit=False, backend=_UnavailableBackend())  # type: ignore[arg-type]
    assert result is None


def test_explicit_docker_available_returns_backend() -> None:
    """When Docker is usable the same backend instance is returned."""
    backend = _AvailableBackend()
    result = attach_docker_backend(explicit=True, backend=backend)  # type: ignore[arg-type]
    assert result is backend


def test_auto_selected_docker_available_returns_backend() -> None:
    """Auto-selection also returns the backend when Docker is usable."""
    backend = _AvailableBackend()
    result = attach_docker_backend(explicit=False, backend=backend)  # type: ignore[arg-type]
    assert result is backend


# ---------------------------------------------------------------------------
# Issue #3039: the gate keys on "a container runtime was named"
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("runtime", sorted(CONTAINER_SANDBOX_RUNTIMES))
def test_explicit_container_runtime_unavailable_raises(runtime: str) -> None:
    """Every accepted container runtime refuses when it cannot be provided."""
    with pytest.raises(SandboxSelectionError) as excinfo:
        attach_container_backend(
            runtime,
            explicit=True,
            backend=_UnavailableBackend(),  # type: ignore[arg-type]
            probe=_unavailable,
        )

    err = excinfo.value
    assert err.attempted == (runtime,)
    # Same shape as the historical docker refusal: name the flag, name the
    # runtime, and say plainly that host execution is being refused.
    assert f"--sandbox {runtime}" in err.reason
    assert "refusing to fall back" in err.reason.lower()


@pytest.mark.parametrize("runtime", sorted(CONTAINER_SANDBOX_RUNTIMES))
def test_auto_selected_container_runtime_unavailable_falls_back_quietly(runtime: str) -> None:
    """Without the explicit bit an unavailable runtime still degrades."""
    result = attach_container_backend(
        runtime,
        explicit=False,
        backend=_UnavailableBackend(),  # type: ignore[arg-type]
        probe=_unavailable,
    )
    assert result is None


@pytest.mark.parametrize("runtime", sorted(CONTAINER_SANDBOX_RUNTIMES))
def test_available_container_runtime_never_raises(runtime: str) -> None:
    """A healthy runtime passes the gate whether or not it has a backend."""
    result = attach_container_backend(
        runtime,
        explicit=True,
        backend=_AvailableBackend(),  # type: ignore[arg-type]
        probe=_available,
    )
    # docker returns its first-party backend; podman rides the CLI sandbox
    # path, so the gate's only job there is to have not raised.
    assert result is None or result.name == "docker"


@pytest.mark.parametrize("runtime", NON_CONTAINER_SANDBOX_CHOICES)
def test_non_container_runtime_is_never_probed_or_refused(runtime: str) -> None:
    """worktree and the cloud backends are left entirely alone by this gate."""
    probed: list[str] = []

    def _tracking_probe(name: str) -> str | None:
        probed.append(name)
        return _unavailable(name)

    result = attach_container_backend(runtime, explicit=True, probe=_tracking_probe)

    assert result is None
    assert probed == []
    assert is_container_runtime(runtime) is False


@pytest.mark.parametrize("raw", ["", "   ", "DOCKER", " Podman "])
def test_runtime_name_is_normalised_before_the_gate_reads_it(raw: str) -> None:
    """Case and padding never decide whether isolation is enforced."""
    assert is_container_runtime(raw) is (raw.strip().lower() in CONTAINER_SANDBOX_RUNTIMES)


def test_container_runtime_set_is_exactly_the_supported_runtimes() -> None:
    """The gate's runtime set matches an independently written expectation.

    The expected set is spelled out here rather than recomputed from
    ``SandboxRuntime``. Comparing the derived set against the expression it
    is defined by is a tautology: it holds for any value, including a typo
    or a runtime no gate can actually probe, so it cannot detect a change.
    Adding a runtime has to fail here first, which forces the addition to be
    reviewed against every consumer pinned below.
    """
    assert frozenset({"docker", "podman"}) == CONTAINER_SANDBOX_RUNTIMES


def test_runtime_consumers_derive_from_the_single_source() -> None:
    """Every former hardcoded copy of the runtime set now derives from one place.

    Issue #3039: the runtime names used to be written down four times - the
    ``SandboxRuntime`` type, the ``sandbox.runtime`` config validator, the
    MCP server sandbox validator, and the CLI's container-implies-``--container``
    test. Only the first extended the explicit-intent gates, so a runtime
    added to one of the others failed open. These must stay derived, not
    re-forked into fresh literals.
    """
    from bernstein.core.protocols.mcp.mcp_sandbox import _VALID_RUNTIMES as mcp_runtimes
    from bernstein.core.security.sandbox import CONTAINER_RUNTIME_NAMES

    assert CONTAINER_SANDBOX_RUNTIMES == CONTAINER_RUNTIME_NAMES
    assert mcp_runtimes == CONTAINER_RUNTIME_NAMES


def test_every_container_runtime_is_offered_by_the_sandbox_flag() -> None:
    """Each canonical container runtime is reachable from ``--sandbox``.

    The direction pinned here is the one that can silently weaken isolation.
    A runtime the gates know about but the flag does not offer is merely dead
    configuration; a container runtime reachable from the flag that the gate
    set does not contain is the #3039 defect, because the explicit-intent
    check returns ``False`` for it and the request fails open.
    """
    from bernstein.cli.run_bootstrap import SANDBOX_CHOICES

    assert frozenset(SANDBOX_CHOICES) >= CONTAINER_SANDBOX_RUNTIMES


def test_orchestrator_attach_gate_does_not_key_on_a_single_runtime_literal() -> None:
    """The orchestrator's wiring-time gate is runtime-agnostic.

    The gate lives in the orchestrator's ``__main__`` block, so it cannot be
    imported and called. Reading the source is the available way to keep the
    exact defect from #3039 - a gate spelled ``_sandbox_runtime == "docker"``,
    which skipped the availability probe for every other runtime - from coming
    back.
    """
    from pathlib import Path

    import bernstein.core.orchestration.orchestrator as orchestrator_module

    source = Path(orchestrator_module.__file__).read_text(encoding="utf-8")
    assert '_sandbox_runtime == "docker"' not in source
    assert "attach_container_backend(_sandbox_runtime, explicit=True)" in source
