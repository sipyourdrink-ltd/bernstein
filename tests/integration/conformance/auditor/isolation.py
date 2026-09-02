"""Running the standalone verifier the way an auditor would run it.

An auditor has the bundle, a laptop, and the published verifier wheel.
They do not have this repository, this virtualenv, or the orchestrator.
Every verification vector therefore leaves the pytest process entirely:
it shells out to ``verify_cli/`` under an interpreter where importing
``bernstein`` fails, so a verifier that quietly grew a dependency on the
product cannot pass a vector here.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

#: Repository root, derived from this file's own location.
REPO_ROOT = Path(__file__).resolve().parents[4]

#: The standalone receipt verifier, run as a plain script.
RECEIPT_VERIFIER = REPO_ROOT / "verify_cli" / "bernstein_verify_receipt" / "verify.py"

#: What the verifier needs, and the complete list of what the isolated
#: interpreter is allowed to have.
VERIFIER_DEPENDENCIES = ("cryptography>=50.0.0", "cbor2>=5.6")

_PYTHON_REL = "Scripts/python.exe" if sys.platform == "win32" else "bin/python"

#: Module name of the guard planted into the isolated interpreter.
AIRGAP_MODULE = "_bernstein_airgap"

#: Planted into the isolated interpreter so "offline" is enforced rather
#: than assumed. A verifier that ever reaches for the network dies here
#: instead of quietly succeeding on a machine that happens to have one.
#:
#: It is installed through a ``.pth`` file rather than ``sitecustomize``:
#: some interpreters (Homebrew's, for one) ship their own
#: ``sitecustomize`` in the stdlib directory, which precedes
#: ``site-packages`` on ``sys.path`` and would shadow ours - leaving the
#: guard silently absent and the air-gap claim untested.
_AIRGAP_GUARD = '''"""Deny every socket: the auditor\'s laptop is air-gapped."""

import socket


class NetworkDenied(RuntimeError):
    """Raised when anything in this interpreter tries to open a socket."""


def _deny(*_args, **_kwargs):
    raise NetworkDenied("network access is denied in the air-gapped verifier interpreter")


socket.socket = _deny
socket.create_connection = _deny
socket.socketpair = _deny
'''


def _plant_airgap(python: Path) -> None:
    """Install the socket-denying guard into *python*'s environment."""
    purelib = Path(
        subprocess.run(
            [str(python), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip(),
    )
    purelib.mkdir(parents=True, exist_ok=True)
    (purelib / f"{AIRGAP_MODULE}.py").write_text(_AIRGAP_GUARD, encoding="utf-8")
    # ``zz`` so the guard is the last .pth processed and cannot be undone
    # by a package that re-imports socket after it.
    (purelib / f"zz_{AIRGAP_MODULE}.pth").write_text(f"import {AIRGAP_MODULE}\n", encoding="utf-8")


def create_isolated_interpreter(venv_dir: Path) -> Path:
    """Create a venv holding only the verifier's dependencies, and no network.

    Args:
        venv_dir: Directory to create the environment in.

    Returns:
        Path to the environment's interpreter.
    """
    uv_bin = shutil.which("uv")
    if uv_bin:
        subprocess.run([uv_bin, "venv", str(venv_dir), "--quiet"], check=True)
        python = venv_dir / _PYTHON_REL
        subprocess.run(
            [uv_bin, "pip", "install", "--quiet", "--python", str(python), *VERIFIER_DEPENDENCIES],
            check=True,
        )
    else:
        venv.create(str(venv_dir), with_pip=True, clear=True)
        python = venv_dir / _PYTHON_REL
        subprocess.run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--quiet",
                "--disable-pip-version-check",
                *VERIFIER_DEPENDENCIES,
            ],
            check=True,
            cwd=str(venv_dir),
        )
    _plant_airgap(python)
    return python


def run_isolated(python: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run *args* under *python* with no inherited import path.

    ``PYTHONPATH`` is stripped so the child cannot pick up the project's
    ``src`` tree, and ``UV_NO_SYNC`` is set so no package manager repairs
    the environment underneath the child.

    Args:
        python: Interpreter to run under.
        *args: Arguments after the interpreter.

    Returns:
        The completed process, never raising on a non-zero exit.
    """
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["UV_NO_SYNC"] = "1"
    return subprocess.run([str(python), *args], capture_output=True, text=True, check=False, env=env)


__all__ = [
    "AIRGAP_MODULE",
    "RECEIPT_VERIFIER",
    "REPO_ROOT",
    "VERIFIER_DEPENDENCIES",
    "create_isolated_interpreter",
    "run_isolated",
]
