"""Verification in an environment that has no bernstein and no network.

The bundle's claim is that a third party can check it without the
machine that produced it. A verification helper that imports
``bernstein`` in the test process proves nothing, so this module builds
an interpreter environment the auditor would actually have and shells
out to ``verify_cli/`` inside it:

* ``-S`` keeps ``site`` from running, so the editable install's ``.pth``
  never puts ``src/`` on the path;
* ``-P`` keeps the working directory off the path;
* ``PYTHONPATH`` carries exactly ``verify_cli/`` plus a directory of
  symlinks to the verifier's declared dependencies - ``bernstein`` is
  not among them, and :func:`probe_import` proves it;
* an audit hook denies every socket call, so "offline" is enforced
  rather than asserted.

The environment is built from the running interpreter's own
site-packages, so no wheel is downloaded and no venv is created: the
check stays fast enough to run on every pull request.
"""

from __future__ import annotations

import os
import subprocess
import sys
import sysconfig
from dataclasses import dataclass
from pathlib import Path

#: Top-level distributions the standalone verifier needs. ``cryptography``
#: and ``cbor2`` are its declared dependencies; the rest are what
#: ``cryptography`` itself loads at import time on some platforms. Nothing
#: else is mirrored, so an accidental dependency on the orchestrator shows
#: up as an ImportError rather than as a silent pass.
VERIFIER_DEPENDENCIES: tuple[str, ...] = (
    "cryptography",
    "cbor2",
    "_cffi_backend",
    "cffi",
    "pycparser",
)

#: Emitted by the audit hook when the verifier reaches for the network.
NETWORK_DENIED_MARKER = "auditor-environment-network-denied"

#: Audited events that would mean the verifier is not working offline.
_NETWORK_EVENTS = (
    "socket.__new__",
    "socket.bind",
    "socket.connect",
    "socket.getaddrinfo",
    "socket.gethostbyname",
    "socket.sendto",
    "urllib.Request",
)

_BOOTSTRAP = f"""
import sys

_DENIED = {_NETWORK_EVENTS!r}


def _deny(event, args):
    if event in _DENIED:
        raise RuntimeError({NETWORK_DENIED_MARKER!r} + ": " + event)


sys.addaudithook(_deny)

mode = sys.argv[1]
if mode == "probe-import":
    __import__(sys.argv[2])
    print("imported " + sys.argv[2])
elif mode == "probe-socket":
    import socket

    socket.socket()
    print("socket opened")
elif mode == "verify-receipt":
    from bernstein_verify_receipt.verify import main

    sys.exit(main(sys.argv[2:]))
else:
    raise SystemExit("unknown mode: " + mode)
"""


@dataclass(frozen=True, slots=True)
class AuditorEnvironment:
    """An interpreter environment with the verifier but not the orchestrator.

    Attributes:
        python: Interpreter to run.
        python_path: Entries handed to the subprocess as ``PYTHONPATH``.
        workdir: Directory the subprocess runs in.
    """

    python: Path
    python_path: tuple[Path, ...]
    workdir: Path

    def env(self) -> dict[str, str]:
        """Return the subprocess environment for this auditor environment."""
        return {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": os.pathsep.join(str(entry) for entry in self.python_path),
            "PYTHONIOENCODING": "utf-8",
        }


@dataclass(frozen=True, slots=True)
class SubprocessResult:
    """What one auditor-environment subprocess produced."""

    returncode: int
    stdout: str
    stderr: str


class AuditorEnvironmentError(RuntimeError):
    """The auditor environment could not be built on this platform."""


def repo_root() -> Path:
    """Return the repository root that holds ``verify_cli/``."""
    return Path(__file__).resolve().parents[3]


def inherited_env() -> dict[str, str]:
    """Return a copy of the current environment for nested tool subprocesses.

    ``uv`` re-synchronises the project's virtualenv when a nested command
    runs under it, which can pull the suite out from under a running
    shard. Pinning ``UV_NO_SYNC`` here keeps a nested invocation read-only
    with respect to the environment it inherits.
    """
    env = dict(os.environ)
    env["UV_NO_SYNC"] = "1"
    return env


def build_environment(workdir: Path) -> AuditorEnvironment:
    """Build an auditor environment under *workdir*.

    Args:
        workdir: Scratch directory the mirror and the subprocess live in.

    Returns:
        The built :class:`AuditorEnvironment`.

    Raises:
        AuditorEnvironmentError: The dependency mirror cannot be created
            (a platform without symlinks), or a declared dependency of the
            standalone verifier is not installed.
    """
    site_packages = Path(sysconfig.get_paths()["purelib"])
    mirror = workdir / "auditor-site-packages"
    mirror.mkdir(parents=True, exist_ok=True)

    mirrored: set[str] = set()
    for entry in sorted(site_packages.iterdir()):
        top_level = entry.name.split(".", 1)[0]
        if top_level not in VERIFIER_DEPENDENCIES:
            continue
        target = mirror / entry.name
        if not target.exists():
            try:
                target.symlink_to(entry)
            except OSError as exc:  # pragma: no cover - platform dependent
                raise AuditorEnvironmentError(f"cannot mirror {entry} into the auditor environment: {exc}") from exc
        mirrored.add(top_level)

    missing = {"cryptography", "cbor2"} - mirrored
    if missing:
        raise AuditorEnvironmentError(f"the standalone verifier's dependencies are not installed: {sorted(missing)}")

    run_dir = workdir / "auditor-cwd"
    run_dir.mkdir(parents=True, exist_ok=True)
    return AuditorEnvironment(
        python=Path(sys.executable),
        python_path=(repo_root() / "verify_cli", mirror),
        workdir=run_dir,
    )


def run(environment: AuditorEnvironment, argv: list[str]) -> SubprocessResult:
    """Run the bootstrap in *environment* with *argv*.

    Args:
        environment: The auditor environment to run inside.
        argv: Bootstrap mode plus its arguments.

    Returns:
        The subprocess result.
    """
    completed = subprocess.run(
        [str(environment.python), "-S", "-P", "-c", _BOOTSTRAP, *argv],
        capture_output=True,
        text=True,
        cwd=environment.workdir,
        env=environment.env(),
        check=False,
    )
    return SubprocessResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def probe_import(environment: AuditorEnvironment, module: str) -> SubprocessResult:
    """Try to import *module* inside the auditor environment."""
    return run(environment, ["probe-import", module])


def probe_socket(environment: AuditorEnvironment) -> SubprocessResult:
    """Try to open a socket inside the auditor environment."""
    return run(environment, ["probe-socket"])


def verify_receipt(
    environment: AuditorEnvironment,
    *,
    receipt: Path,
    trust_anchor: Path | None = None,
) -> SubprocessResult:
    """Verify *receipt* with ``verify_cli/`` inside the auditor environment.

    Args:
        environment: The auditor environment to verify inside.
        receipt: Path to the receipt taken from the bundle.
        trust_anchor: Optional operator public key, supplied out of band.
            When given, the receipt's embedded key must match it, so a
            bundle re-signed with an unrelated key fails instead of
            trusting itself.

    Returns:
        The subprocess result; ``returncode == 0`` is a pass.
    """
    argv = ["verify-receipt", "--receipt", str(receipt), "--format", "all", "--verbose"]
    if trust_anchor is not None:
        argv += ["--public-key", str(trust_anchor)]
    return run(environment, argv)


__all__ = [
    "NETWORK_DENIED_MARKER",
    "VERIFIER_DEPENDENCIES",
    "AuditorEnvironment",
    "AuditorEnvironmentError",
    "SubprocessResult",
    "build_environment",
    "inherited_env",
    "probe_import",
    "probe_socket",
    "repo_root",
    "run",
    "verify_receipt",
]
