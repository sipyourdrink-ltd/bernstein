"""Egress containment, verified against a real container rather than a mock.

The unit tests prove the profile *decides* deny-all plus an explicit list.  That
is a different claim from "a stranger's code cannot reach a host the project
did not name", and only the second one matters to a donor.  So these tests
start actual containers from the options
:func:`~bernstein.core.volunteer.sandbox_profile.backend_options` produces and
ask the container what it can see.

Skipped without a Docker daemon.  A skipped containment test is honest; a
mocked one is not.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

import pytest

from bernstein.core.volunteer.manifest import load_manifest
from bernstein.core.volunteer.sandbox_profile import (
    backend_options,
    build_volunteer_profile,
    sandbox_env,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]

IMAGE = "alpine:3"

#: A documentation-reserved address (RFC 5737).  Nothing routes there, which is
#: what we want: the test asks whether a *name resolves*, not whether a host
#: answers.  Reaching out to a real service would make the test depend on
#: somebody else's uptime.
ALLOWED_HOST = "allowed.test"
ALLOWED_ADDRESS = "203.0.113.7"

#: A name the manifest never lists.  Real and famously resolvable, so a failure
#: to resolve it is evidence about the sandbox rather than about the name.
FORBIDDEN_HOST = "example.com"


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        probe = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0


requires_docker = pytest.mark.skipif(not _docker_available(), reason="needs a running Docker daemon")


def _manifest(**overrides: Any) -> Any:
    payload: dict[str, Any] = {
        "version": 1,
        "license": "Apache-2.0",
        "gates": [["true"]],
        "sandbox": "container",
        "max_wall_clock_minutes": 5,
    }
    payload.update(overrides)
    return load_manifest(json.dumps(payload))


def _profile(**overrides: Any) -> Any:
    return build_volunteer_profile(
        _manifest(**overrides),
        available_backends=("container",),
        donor_accepts_plain_container=True,
    )


def _docker_argv(options: dict[str, Any], *, command: str, env: dict[str, str] | None = None) -> list[str]:
    """Turn profile options into a ``docker run`` invocation.

    Deliberately mechanical and deliberately here: the test builds the command
    from the options the profile produced, so a profile that stops emitting a
    control stops applying it here too.  A hand-written docker command would
    keep passing after the profile lost the field.
    """
    argv = ["docker", "run", "--rm"]
    if options["network_disabled"]:
        argv += ["--network", "none"]
    else:
        for server in options["dns"]:
            argv += ["--dns", server]
        for host, address in options["extra_hosts"].items():
            argv += ["--add-host", f"{host}:{address}"]
    argv += ["--memory", f"{options['memory_mb']}m"]
    for key, value in (env or {}).items():
        argv += ["--env", f"{key}={value}"]
    argv += [IMAGE, "sh", "-c", command]
    return argv


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


@requires_docker
def test_a_host_the_project_listed_resolves_inside_the_sandbox() -> None:
    """The allowlist has to permit something, or projects cannot run gates."""
    options = backend_options(
        _profile(egress_allowlist=[ALLOWED_HOST]),
        resolved_hosts={ALLOWED_HOST: ALLOWED_ADDRESS},
    )

    result = _run(_docker_argv(options, command=f"getent hosts {ALLOWED_HOST}"))

    assert result.returncode == 0, result.stderr
    assert ALLOWED_ADDRESS in result.stdout


@requires_docker
def test_a_host_the_project_did_not_list_does_not_resolve_inside_the_sandbox() -> None:
    """The containment claim, asked of the container instead of the dataclass.

    The sandbox is given a resolver that answers nothing and static entries for
    the allowlist only, so a name outside the list has nowhere to come from.
    """
    options = backend_options(
        _profile(egress_allowlist=[ALLOWED_HOST]),
        resolved_hosts={ALLOWED_HOST: ALLOWED_ADDRESS},
    )

    result = _run(_docker_argv(options, command=f"getent hosts {FORBIDDEN_HOST}"))

    assert result.returncode != 0, f"{FORBIDDEN_HOST} resolved inside the sandbox: {result.stdout!r}"


@requires_docker
def test_a_project_that_lists_no_host_gets_no_network_interface() -> None:
    """The complete case, and the one a project should choose when it can.

    DNS pinning does not stop a connection to a raw IP address.  Turning the
    interface off does.
    """
    import dataclasses

    options = backend_options(dataclasses.replace(_profile(), egress_allowlist=()))
    assert options["network_disabled"] is True

    result = _run(_docker_argv(options, command="ip -o addr show | grep -v ' lo ' | wc -l"))

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0", f"the sandbox has a network interface: {result.stdout!r}"


@requires_docker
def test_the_sandbox_environment_carries_no_credential_the_host_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The canary test, taken all the way into the container.

    The unit test proves ``sandbox_env`` does not build the value.  This proves
    the value is not inside the running sandbox, which is the claim a donor
    actually cares about.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-canary-do-not-leak")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_canary_do_not_leak")

    profile = _profile()
    options = backend_options(_without_network(profile))

    result = _run(_docker_argv(options, command="env", env=sandbox_env(profile)))

    assert result.returncode == 0, result.stderr
    assert "canary" not in result.stdout
    assert "ANTHROPIC_API_KEY" not in result.stdout
    assert "GITHUB_TOKEN" not in result.stdout
    assert "HOME=/workspace" in result.stdout


def _without_network(profile: Any) -> Any:
    """The canary check needs no network, so it does not get one."""
    import dataclasses

    return dataclasses.replace(profile, egress_allowlist=())
