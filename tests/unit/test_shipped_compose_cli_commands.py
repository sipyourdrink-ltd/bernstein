"""Guard against issue #3005: shipped docker-compose files invoking a
``bernstein`` CLI subcommand that does not exist (e.g. the historical
``python -m bernstein.cli server`` / ``bernstein cluster server``).

For every compose file this repo ships as an operator-facing example or
deployment, this resolves the effective container entrypoint for each
service, and - for services whose container execs the ``bernstein`` binary -
walks the resulting argv against the *real* Click command tree defined in
``bernstein.cli.main``. A future rename of a subcommand (e.g. ``serve`` ->
``server``) without updating these files fails this test instead of silently
shipping a container that crash-loops on first ``up``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from click.core import Group

from bernstein.cli.main import cli

REPO_ROOT = Path(__file__).resolve().parents[2]

# Directories that never hold an operator-facing compose file.
_EXCLUDED_DIRS = frozenset({".git", ".venv", "venv", "node_modules", ".tox", ".mypy_cache"})

# Filename shapes Compose itself recognises, so a file named `compose.yaml`
# is covered the same as `docker-compose.yaml`.
_COMPOSE_GLOBS = ("docker-compose*.yaml", "docker-compose*.yml", "compose.yaml", "compose.yml")


def _discover_shipped_compose_files() -> list[Path]:
    """Find every compose file this repo ships, by walking the tree.

    Discovered rather than hand-listed: a manually maintained list silently
    stops covering compose files added later, which turns this guard green on
    a file it never actually checked. Anything matching a Compose filename
    outside the excluded directories is in scope automatically.
    """
    found: set[Path] = set()
    for pattern in _COMPOSE_GLOBS:
        for path in REPO_ROOT.rglob(pattern):
            if _EXCLUDED_DIRS.isdisjoint(path.relative_to(REPO_ROOT).parts):
                found.add(path)
    return sorted(found)


SHIPPED_COMPOSE_FILES: list[Path] = _discover_shipped_compose_files()

# A glob that silently matches nothing would make every parametrized case
# below vacuous, so assert discovery actually found the shipped files.
assert SHIPPED_COMPOSE_FILES, f"no shipped compose files discovered under {REPO_ROOT}"

# Dummy values for the `${VAR:?required}` env vars some of these compose
# files declare, so `docker compose config` can resolve them without a real
# secret. These never reach a running container in this test.
_PLACEHOLDER_ENV = {
    "TS_AUTHKEY": "tskey-placeholder-for-config-validation",
    "BERNSTEIN_CLUSTER_AUTH_SECRET": "placeholder-for-config-validation",
    "CF_TUNNEL_TOKEN": "placeholder-for-config-validation",
    "BERNSTEIN_AUTH_TOKEN": "placeholder-for-config-validation",
}


def _load_yaml(path: Path) -> dict[str, object]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(doc, dict), f"{path} did not parse to a YAML mapping"
    return doc


def _tokens(value: object) -> list[str]:
    """Normalize a compose `command:`/`entrypoint:` value to argv tokens.

    Compose accepts either a YAML list or a shell-style string (including
    multi-line `>` block scalars); both forms appear in this repo's files.
    """
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        return value.split()
    raise TypeError(f"unexpected command/entrypoint shape: {value!r}")


def _dockerfile_entrypoint_tokens(dockerfile_path: Path) -> list[str] | None:
    """Return the argv of the last ``ENTRYPOINT [...]`` line in a Dockerfile.

    Docker uses the last ENTRYPOINT instruction when several are present.
    Returns None if the file is missing, unreadable, or has no JSON-array
    ENTRYPOINT line (shell-form ENTRYPOINT is out of scope - none of this
    repo's shipped Dockerfiles use it).
    """
    if not dockerfile_path.is_file():
        return None
    last_entrypoint: str | None = None
    for line in dockerfile_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("ENTRYPOINT "):
            last_entrypoint = stripped[len("ENTRYPOINT ") :].strip()
    if last_entrypoint is None:
        return None
    try:
        parsed = json.loads(last_entrypoint)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, list) and all(isinstance(item, str) for item in parsed):
        return [str(item) for item in parsed]
    return None


def _builds_bernstein_image(compose_file: Path, service: dict[str, object]) -> bool:
    """True if this service's container image execs the ``bernstein`` binary
    by default - i.e. a service whose command is worth validating against
    the Bernstein CLI, as opposed to postgres/redis/caddy/tailscale/
    cloudflared/otel-collector/etc., which have entirely different images.
    """
    build = service.get("build")
    if isinstance(build, dict):
        context = str(build.get("context", "."))
        dockerfile = str(build.get("dockerfile", "Dockerfile"))
        dockerfile_path = (compose_file.parent / context / dockerfile).resolve()
        tokens = _dockerfile_entrypoint_tokens(dockerfile_path)
        return tokens is not None and tokens[:1] == ["bernstein"]
    image = service.get("image")
    return isinstance(image, str) and "sipyourdrink-ltd/bernstein" in image


def _resolved_argv(compose_file: Path, service: dict[str, object]) -> list[str] | None:
    """Return the full argv the container's PID 1 would exec (binary +
    args), or None if this service does not exec the ``bernstein`` CLI at
    all - either because it overrides `entrypoint:` to something else
    (uvicorn, a Python module, a third-party binary), or because it has no
    `command:` override, or because it isn't built from the bernstein image.
    """
    entrypoint = service.get("entrypoint")
    command = _tokens(service.get("command"))

    if entrypoint is not None:
        entry_tokens = _tokens(entrypoint)
        if not entry_tokens or entry_tokens[0] != "bernstein":
            return None
        return entry_tokens + command

    if not command:
        return None
    if not _builds_bernstein_image(compose_file, service):
        return None
    # No entrypoint override: falls through to the image's own ENTRYPOINT,
    # which _builds_bernstein_image just confirmed execs `bernstein`.
    return ["bernstein", *command]


def _walk_cli_tree(argv_after_binary: list[str]) -> tuple[bool, str]:
    """Walk argv (everything after the `bernstein` binary name) against the
    real Click command tree rooted at `bernstein.cli.main.cli`.

    Stops at the first token that looks like a flag (`-...`), or once it
    resolves to a leaf command (which may itself take positional args this
    guard doesn't need to understand). Returns (ok, human-readable detail).
    """
    group: Group = cli
    consumed: list[str] = []
    for token in argv_after_binary:
        if token.startswith("-"):
            break
        consumed.append(token)
        command = group.commands.get(token) if isinstance(group, Group) else None
        if command is None:
            path = " ".join(consumed)
            return False, f"`bernstein {path}` is not a registered CLI command"
        if isinstance(command, Group):
            group = command
            continue
        path = " ".join(consumed)
        return True, f"`bernstein {path}` resolves to a real command"
    path = " ".join(consumed)
    return True, f"`bernstein {path}` resolves to a real command group"


def _bernstein_invocations(compose_file: Path) -> list[tuple[str, list[str]]]:
    """Return (service_name, argv_after_binary) for every service in this
    compose file whose container execs the bernstein CLI."""
    doc = _load_yaml(compose_file)
    services = doc.get("services", {})
    assert isinstance(services, dict), f"{compose_file}: `services:` is not a mapping"
    found: list[tuple[str, list[str]]] = []
    for name, service in services.items():
        if not isinstance(service, dict):
            continue
        resolved = _resolved_argv(compose_file, service)
        if resolved is not None:
            found.append((name, resolved[1:]))
    return found


def _docker_compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(
        ["docker", "compose", "version"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return probe.returncode == 0


@pytest.mark.parametrize(
    "compose_file",
    SHIPPED_COMPOSE_FILES,
    ids=[str(p.relative_to(REPO_ROOT)) for p in SHIPPED_COMPOSE_FILES],
)
def test_shipped_compose_file_parses(compose_file: Path) -> None:
    """Every shipped compose file exists, is valid YAML, and - when the
    `docker compose` CLI is available - passes `docker compose config`."""
    assert compose_file.is_file(), f"missing shipped compose file: {compose_file}"

    _load_yaml(compose_file)  # always at least YAML-parses

    if not _docker_compose_available():
        pytest.skip("docker compose CLI not available in this test environment")

    result = subprocess.run(
        ["docker", "compose", "-f", str(compose_file), "config", "-q"],
        cwd=compose_file.parent,
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, **_PLACEHOLDER_ENV},
        check=False,
    )
    assert result.returncode == 0, (
        f"`docker compose -f {compose_file.relative_to(REPO_ROOT)} config` failed:\n{result.stderr}"
    )


@pytest.mark.parametrize(
    "compose_file",
    SHIPPED_COMPOSE_FILES,
    ids=[str(p.relative_to(REPO_ROOT)) for p in SHIPPED_COMPOSE_FILES],
)
def test_shipped_compose_commands_resolve_to_real_cli_subcommands(compose_file: Path) -> None:
    """Every `command:`/`entrypoint:` that execs the bernstein CLI in a
    shipped compose file must name a subcommand that actually exists.

    This is the regression guard for issue #3005: two families of compose
    files invoked `python -m bernstein.cli server` and `bernstein cluster
    server`, neither of which is a real command, so the container exited
    immediately on every `up`.
    """
    for service_name, argv in _bernstein_invocations(compose_file):
        ok, detail = _walk_cli_tree(argv)
        assert ok, (
            f"{compose_file.relative_to(REPO_ROOT)} service {service_name!r}: {detail}. "
            "Update the compose file's command: to invoke a real `bernstein` "
            "CLI subcommand (cross-check with `bernstein --help`)."
        )


def test_guard_actually_inspects_the_known_bernstein_services() -> None:
    """Sanity-check the extraction logic itself finds the services this
    guard exists to protect, so a bug in `_resolved_argv`/
    `_builds_bernstein_image` (e.g. a broken path) can't make every check
    above pass vacuously by finding zero services anywhere.
    """
    expected_services_by_file = {
        REPO_ROOT / "docker" / "sandbox" / "docker-compose.yaml": {"bernstein-server"},
        REPO_ROOT / "docker" / "sandbox" / "docker-compose.researcher.yaml": {"bernstein-server"},
        REPO_ROOT / "examples" / "cluster" / "tailscale" / "docker-compose.yml": {"bernstein-central"},
        REPO_ROOT / "examples" / "cluster" / "cloudflared" / "docker-compose.yml": {"bernstein-central"},
    }
    for compose_file, expected_services in expected_services_by_file.items():
        found_services = {name for name, _ in _bernstein_invocations(compose_file)}
        assert found_services == expected_services, (
            f"{compose_file.relative_to(REPO_ROOT)}: expected to find bernstein-CLI "
            f"services {sorted(expected_services)}, found {sorted(found_services)} - "
            "the extraction logic in this test may have regressed"
        )
