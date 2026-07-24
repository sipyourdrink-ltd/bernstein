"""libkrun-backed microVM monitor for the ``microvm`` sandbox backend (#2971).

:class:`LibkrunMonitor` satisfies the same
:class:`~bernstein.core.sandbox.backends._vmmonitor.VMMonitor` protocol as
:class:`~bernstein.core.sandbox.backends._vmmonitor.FirecrackerMonitor` and
:class:`~bernstein.core.sandbox.backends._vmmonitor.FakeMonitor`, so everything
above the seam - the backend, the content-addressed snapshots, the fork-race
and the selection receipts - is unchanged.

Why libkrun
-----------
libkrun *is* the L1 hypervisor (KVM on Linux, Hypervisor.framework on
macOS/arm64), so it needs no nested virtualisation and boots on ordinary
developer hardware. It also exposes ``krun_add_virtiofs3()``, which lets the
session workspace be a real host directory passed through to the guest. That
makes :meth:`LibkrunMonitor.read_file`, :meth:`LibkrunMonitor.write_file`,
:meth:`LibkrunMonitor.ls`, :meth:`LibkrunMonitor.freeze_image` and
:meth:`LibkrunMonitor.restore_image` plain host filesystem operations - no
guest agent, no framing protocol, no watchdog. ``freeze_image`` reuses
:func:`~bernstein.core.sandbox.backends._vmmonitor.canonical_workspace_image`
unchanged, so its bytes (and therefore the content address) are identical to
the ones ``FakeMonitor`` produces for the same tree, with no second
implementation to drift.

Three shares make up a guest:

===============  ==========================  =====================================
virtio-fs tag    host directory              guest mount
===============  ==========================  =====================================
``/dev/root``    operator-supplied rootfs    ``/`` (mounted by libkrun's own init)
``bnsws``        session workspace           the monitor's logical root
``bnsctl``       per-exec control directory  :data:`GUEST_CONTROL_DIR`
===============  ==========================  =====================================

The control directory carries stdin/stdout/stderr and the status report. It is
deliberately *outside* the workspace: a snapshot must contain the workspace and
nothing else, or ``freeze_image`` would stop matching ``FakeMonitor``.

The two bernstein shares are attached with ``KRUN_SEMANTICS_LINUX_SIMPLIFIED``,
which stores permission bits in the host inode rather than an extended
attribute. The workspace is snapshotted by reading it from the host, so the two
views have to agree: under the default semantics a file the guest creates 0644
lands on the host 0600, and freezing it would silently drop the mode - an
executable the guest built would restore non-executable.

Process model
-------------
``krun_start_enter()`` never returns - the VMM takes over the calling process
and ``exit()``s with the workload's status - so one process runs one VM.
On macOS there is a second, independent reason the VM cannot live in the
orchestrator process: ``hv_vm_create()`` fails with ``EINVAL`` unless the
*running executable image* carries ``com.apple.security.hypervisor``, and a
framework CPython cannot usefully carry it. Both constraints are satisfied by
the same artifact: a small signed launcher binary (``krunlaunch``, source in
``_libkrun_launcher/``) that :meth:`LibkrunMonitor.exec` spawns per call. Its
absence is reported by :meth:`LibkrunMonitor.preflight` exactly like a missing
hypervisor; build it with :func:`build_launcher` or
``bernstein sandbox microvm-launcher``.

Session state lives entirely in the shared workspace rather than in VM memory,
which is what makes a short-lived VM per ``exec`` semantically correct.

Exit-code disambiguation
------------------------
libkrun reserves 125/126/127 for *its own* failures (see
:data:`LIBKRUN_RESERVED_EXIT_CODES`), and a guest command can legitimately
return those same values. Trusting the launcher's exit code alone would report
a VM-level failure as a guest result, or the reverse. The guest wrapper
therefore writes an explicit status line into the control share *after* the
command completes, and :func:`resolve_guest_exit_code` treats that file - not
the process exit code - as authoritative. A guest command returning 127 is
reported as ``exit_code=127``; a libkrun 127 (guest entrypoint not found)
leaves no status file and raises
:class:`~bernstein.core.sandbox.backends._vmmonitor.MicroVMUnavailableError`.

No silent downgrade
-------------------
On an unsupported host :meth:`LibkrunMonitor.preflight` names every missing
precondition and :meth:`LibkrunMonitor.boot` raises
:class:`~bernstein.core.sandbox.backends._vmmonitor.MicroVMUnavailableError`.
It never falls back to a weaker isolation mode - an operator who asked for a
hardware boundary gets an error, not a surprise.

Isolation caveat
----------------
The hardware boundary is real: a separate kernel, a separate process tree, a
separate network stack. It is not a complete answer. The guest and the VMM
share a security context, so a hypervisor escape lands with the privileges of
the process that spawned the launcher, and virtio-fs does not itself constrain
access beyond the directories that were shared - the guest root share is
writable, so a guest can modify the rootfs directory it was given. Use a rootfs
directory dedicated to sandboxing, and keep host-side confinement of the VMM
process (user namespaces / seccomp on Linux) an operator responsibility.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes.util
import os
import posixpath
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Final

from bernstein.core.sandbox.backend import ExecResult
from bernstein.core.sandbox.backends._vmmonitor import (
    MicroVMUnavailableError,
    canonical_workspace_image,
    extract_workspace_image,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence
    from importlib.resources.abc import Traversable

# ---------------------------------------------------------------------------
# Host configuration surface
# ---------------------------------------------------------------------------

#: Env var pointing at the guest root filesystem (a host *directory*, exposed
#: to the guest as ``/`` through virtio-fs).
ROOTFS_ENV: Final = "BERNSTEIN_MICROVM_LIBKRUN_ROOTFS"
#: Env var overriding the libkrun shared-library path.
LIBRARY_ENV: Final = "BERNSTEIN_MICROVM_LIBKRUN_LIB"
#: Env var pointing at a prebuilt, signed ``krunlaunch`` binary.
LAUNCHER_ENV: Final = "BERNSTEIN_MICROVM_LIBKRUN_LAUNCHER"
#: Env var overriding the guest vCPU count.
VCPUS_ENV: Final = "BERNSTEIN_MICROVM_LIBKRUN_VCPUS"
#: Env var overriding the guest RAM allocation, in MiB.
RAM_MIB_ENV: Final = "BERNSTEIN_MICROVM_LIBKRUN_RAM_MIB"
#: Env var overriding the virtio-fs DAX window size, in bytes.
SHM_BYTES_ENV: Final = "BERNSTEIN_MICROVM_LIBKRUN_SHM_BYTES"

#: macOS entitlement required to call ``hv_vm_create`` (Hypervisor.framework).
HYPERVISOR_ENTITLEMENT: Final = "com.apple.security.hypervisor"
#: macOS entitlement required for libkrun to ``dlopen`` libkrunfw.
LIBRARY_VALIDATION_ENTITLEMENT: Final = "com.apple.security.cs.disable-library-validation"

#: virtio-fs tag for the session workspace.
WORKSPACE_TAG: Final = "bnsws"
#: virtio-fs tag for the per-exec control directory.
CONTROL_TAG: Final = "bnsctl"
#: Guest mount point for the control directory (never part of a snapshot).
GUEST_CONTROL_DIR: Final = "/.bernstein-control"
#: Interpreter the guest wrapper runs under, relative to the guest root.
GUEST_SHELL: Final = "/bin/sh"
#: File name of the launcher binary.
LAUNCHER_NAME: Final = "krunlaunch"

_DEFAULT_VCPUS: Final = 1
_DEFAULT_RAM_MIB: Final = 512
#: 512 MiB DAX window per share. Sizes from 0 to 4 GiB behave identically here;
#: this one is comfortably inside what Hypervisor.framework accepts for three
#: simultaneous shares.
_DEFAULT_SHM_BYTES: Final = 1 << 29

#: Exit codes libkrun's own ``init`` reserves for VM-level failures. A guest
#: command may legitimately return any of these too, which is exactly why the
#: status file - not the process exit code - decides (see
#: :func:`resolve_guest_exit_code`).
LIBKRUN_RESERVED_EXIT_CODES: Final[dict[int, str]] = {
    125: 'libkrun "init" could not set up the environment inside the microVM',
    126: 'libkrun "init" found the guest entrypoint but could not execute it',
    127: 'libkrun "init" could not find the guest entrypoint',
}

#: Marker the guest wrapper writes to report the command's real exit code.
#: Namespaced and explicit so a truncated or absent file can never be mistaken
#: for a successful report.
STATUS_PREFIX: Final = "bernstein-guest-status:"

_CONTROL_STDIN: Final = "stdin"
_CONTROL_STDOUT: Final = "stdout"
_CONTROL_STDERR: Final = "stderr"
_CONTROL_STATUS: Final = "status"
_CONTROL_ENV: Final = "env.sh"
_CONTROL_SCRIPT: Final = "run.sh"
_CONTROL_CONSOLE: Final = "console"

#: How much launcher stderr / guest console to quote when a VM fails.
_DIAGNOSTIC_TAIL_CHARS: Final = 800


# ---------------------------------------------------------------------------
# Host discovery (read-only - safe to call from preflight)
# ---------------------------------------------------------------------------


def _lib_dirs() -> tuple[str, ...]:
    """Well-known directories holding the libkrun / libkrunfw shared objects."""
    if sys.platform == "darwin":
        return ("/opt/homebrew/lib", "/usr/local/lib")
    return ("/usr/lib64", "/usr/lib", "/usr/local/lib")


def _lib_names(stem: str) -> tuple[str, ...]:
    """Candidate file names for a shared object, newest soname first."""
    if sys.platform == "darwin":
        return (f"{stem}.dylib",)
    return (f"{stem}.so.1", f"{stem}.so.5", f"{stem}.so")


def _find_shared_object(stem: str, search: tuple[str, ...] = ()) -> str | None:
    """Locate a shared object without loading it.

    Deliberately does *not* ``dlopen`` anything: :meth:`LibkrunMonitor.preflight`
    must stay side-effect-free, and loading a hypervisor library is not that.
    """
    for directory in (*search, *_lib_dirs()):
        for name in _lib_names(stem):
            candidate = Path(directory) / name
            if candidate.exists():
                return str(candidate)
    # find_library resolves a name against the loader's search path without
    # loading it.
    return ctypes.util.find_library(stem.removeprefix("lib"))


def find_libkrun(explicit: str | None = None) -> str | None:
    """Locate the libkrun shared library, or ``None`` when it is absent."""
    if explicit:
        return explicit if Path(explicit).exists() else None
    return _find_shared_object("libkrun")


def find_libkrunfw(library: str | None = None) -> str | None:
    """Locate libkrunfw (the packaged guest kernel), or ``None`` when absent.

    libkrun ``dlopen``s it at VM-start time rather than linking it, so a host
    with libkrun but no libkrunfw preflights as ready and then fails at boot
    with an opaque loader error. Checking for it here turns that into a named
    precondition.

    libkrun's own directory is searched first: an operator who pointed
    ``$BERNSTEIN_MICROVM_LIBKRUN_LIB`` at a non-standard prefix has almost
    certainly put the guest kernel beside it, and reporting that host as
    missing libkrunfw would be wrong.
    """
    beside = libkrun_library_dir(library)
    return _find_shared_object("libkrunfw", (beside,) if beside else ())


def libkrun_library_dir(library: str | None = None) -> str | None:
    """Directory holding the libkrun shared object, for ``-L`` and ``-rpath``.

    The launcher must carry an rpath to this directory: code signing strips
    ``DYLD_*`` from the environment, so a signed binary that relies on
    ``DYLD_LIBRARY_PATH`` cannot resolve libkrunfw at runtime. Resolving it from
    the located library keeps a hardcoded ``/opt/homebrew`` out of the build.

    Symlinks are deliberately *not* followed. A package manager links both
    libkrun and libkrunfw into one directory while their real files sit in
    per-version directories; resolving would yield a directory holding libkrun
    alone, and the rpath would miss the guest kernel.

    Returns:
        The directory, or ``None`` when the library was found by soname alone
        (already on the loader's default search path, so no rpath is needed).
    """
    resolved = find_libkrun(library)
    if resolved is None or os.sep not in resolved:
        return None
    return str(Path(resolved).parent)


def launcher_source_dir() -> Traversable:
    """Packaged directory holding ``krunlaunch.c`` and its entitlements."""
    return resources.files("bernstein.core.sandbox.backends") / "_libkrun_launcher"


def default_launcher_path() -> Path:
    """Where a built launcher is cached, honouring ``$XDG_CACHE_HOME``."""
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return base / "bernstein" / "libkrun" / LAUNCHER_NAME


def launcher_path(explicit: str | None = None) -> Path:
    """Resolve the launcher location (explicit > env > cache)."""
    override = explicit or os.environ.get(LAUNCHER_ENV)
    return Path(override).expanduser() if override else default_launcher_path()


def launcher_entitlements(path: Path) -> frozenset[str] | None:
    """Entitlements embedded in *path*'s code signature.

    Read-only: it shells out to ``codesign -d``, which only inspects.

    Returns:
        The entitlement keys found, or ``None`` when the check itself could not
        run (no ``codesign``, timeout) - the caller reports "cannot verify"
        rather than guessing either way.
    """
    if shutil.which("codesign") is None:
        return None
    try:
        # Fixed argv, no shell: this only inspects an existing signature.
        proc = subprocess.run(
            ["codesign", "-d", "--entitlements", "-", "--xml", str(path)],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    blob = (proc.stdout + proc.stderr).decode("utf-8", "replace")
    return frozenset(
        entitlement for entitlement in (HYPERVISOR_ENTITLEMENT, LIBRARY_VALIDATION_ENTITLEMENT) if entitlement in blob
    )


# ---------------------------------------------------------------------------
# Launcher build (the only step that writes anything - never called from
# preflight, which must stay side-effect-free)
# ---------------------------------------------------------------------------


def build_launcher(
    *,
    dest: Path | None = None,
    library: str | None = None,
    compiler: str | None = None,
) -> Path:
    """Compile and (on macOS) sign the ``krunlaunch`` binary.

    Args:
        dest: Output path. Defaults to :func:`launcher_path`.
        library: Explicit libkrun path, used to derive the link prefix.
        compiler: C compiler to invoke. Defaults to ``$CC`` then ``cc``.

    Returns:
        The path to the built launcher.

    Raises:
        MicroVMUnavailableError: When libkrun is not installed, no compiler is
            available, or the compile/sign step fails. The message carries the
            tool's own diagnostics so the operator can act on it.
    """
    if find_libkrun(library) is None:
        msg = (
            "cannot build the libkrun launcher: the libkrun shared library was not found "
            f"(install libkrun, or set ${LIBRARY_ENV})"
        )
        raise MicroVMUnavailableError(msg)
    lib_dir = libkrun_library_dir(library)

    cc = compiler or os.environ.get("CC") or "cc"
    if shutil.which(cc) is None:
        msg = f"cannot build the libkrun launcher: C compiler {cc!r} not found on PATH"
        raise MicroVMUnavailableError(msg)

    target = (dest or launcher_path()).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)

    with resources.as_file(launcher_source_dir()) as source_dir:
        _compile_launcher(cc, source_dir / "krunlaunch.c", target, lib_dir)
        if sys.platform == "darwin":
            _sign_launcher(source_dir / "krunlaunch.entitlements.plist", target)
    return target


def _compile_launcher(cc: str, source: Path, target: Path, lib_dir: str | None) -> None:
    """Link the launcher against libkrun, with an rpath to *lib_dir*."""
    argv = [cc, "-O2", "-o", str(target), str(source)]
    if lib_dir is not None:
        # Signed binaries lose DYLD_*, so the library search path has to be
        # baked in at link time or libkrunfw cannot be resolved at runtime.
        argv += [f"-L{lib_dir}", f"-Wl,-rpath,{lib_dir}"]
    argv.append("-lkrun")
    _run_tool(argv, "compiling the libkrun launcher")


def _sign_launcher(entitlements: Path, target: Path) -> None:
    """Ad-hoc sign *target* with the hypervisor entitlements.

    Ad-hoc (``--sign -``) is sufficient for Hypervisor.framework; it is what
    the packaged ``krunkit`` binary does.
    """
    argv = [
        "codesign",
        "--force",
        "--sign",
        "-",
        "--entitlements",
        str(entitlements),
        str(target),
    ]
    _run_tool(argv, "signing the libkrun launcher")


def _run_tool(argv: list[str], what: str) -> None:
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=300, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        msg = f"failed while {what}: {exc}"
        raise MicroVMUnavailableError(msg) from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).decode("utf-8", "replace").strip()
        msg = f"failed while {what} (exit {proc.returncode}): {detail}"
        raise MicroVMUnavailableError(msg)


# ---------------------------------------------------------------------------
# Exit-code disambiguation (pure - unit-testable on every host)
# ---------------------------------------------------------------------------


def parse_guest_status(raw: str | None) -> int | None:
    """Extract the guest command's exit code from a status file's contents.

    Args:
        raw: The status file's text, or ``None`` when it does not exist.

    Returns:
        The reported exit code, or ``None`` when the wrapper never got far
        enough to report one (absent, truncated, or malformed file). ``None``
        always means "no guest result", never "the guest returned 0".
    """
    if raw is None:
        return None
    for line in raw.splitlines():
        candidate = line.strip()
        if not candidate.startswith(STATUS_PREFIX):
            continue
        payload = candidate[len(STATUS_PREFIX) :].strip()
        if not payload.isdigit():
            return None
        value = int(payload)
        # A POSIX wait status carries 8 bits; anything else means the file was
        # not written by our wrapper and must not be trusted as a result.
        return value if 0 <= value <= 255 else None
    return None


def resolve_guest_exit_code(
    *,
    launcher_exit_code: int,
    status_raw: str | None,
    diagnostics: str = "",
) -> int:
    """Decide whether a finished VM carried a guest result or a VM failure.

    This is the correctness-critical half of the adapter. The launcher's exit
    code is ambiguous by construction: libkrun reserves 125/126/127 for its own
    failures and a guest command can return those values too. The status file
    resolves the ambiguity out of band - the guest wrapper writes it only
    *after* the command completed, so its presence proves the command ran and
    its payload is the command's real exit code.

    Args:
        launcher_exit_code: Exit code of the process that ran the VM.
        status_raw: Contents of the guest-written status file, or ``None``.
        diagnostics: Launcher stderr / guest console tail, quoted on failure.

    Returns:
        The guest command's exit code.

    Raises:
        MicroVMUnavailableError: When the guest command never ran to
            completion, naming the libkrun-level reason when the reserved exit
            code identifies one.
    """
    guest_rc = parse_guest_status(status_raw)
    if guest_rc is not None:
        # Authoritative: the wrapper ran the command and reported its status.
        # This is the branch that keeps a *guest* 125/126/127 a guest result.
        return guest_rc

    suffix = f" Diagnostics: {diagnostics.strip()}" if diagnostics.strip() else ""
    reserved = LIBKRUN_RESERVED_EXIT_CODES.get(launcher_exit_code)
    if reserved is not None:
        msg = (
            f"microVM (libkrun) failed before the guest command produced a result: {reserved} "
            f"(reserved exit code {launcher_exit_code}). The guest root filesystem must provide "
            f"{GUEST_SHELL} and a mount(8) that supports virtiofs.{suffix}"
        )
        raise MicroVMUnavailableError(msg)
    msg = (
        f"microVM (libkrun) guest produced no status report; the VM process exited "
        f"{launcher_exit_code}. The guest command's result is unknown, so it is reported as a "
        f"VM failure rather than guessed from the process exit code.{suffix}"
    )
    raise MicroVMUnavailableError(msg)


# ---------------------------------------------------------------------------
# Guest-side program
# ---------------------------------------------------------------------------


def build_bootstrap_argument() -> str:
    """The ``sh -c`` program libkrun starts the guest with.

    Kept deliberately tiny and constant. Everything libkrun is asked to start
    the guest with travels on the kernel command line, which has a hard size
    limit - overrunning it aborts the VMM rather than returning an error. So
    this only mounts the control share and hands over to a script that lives
    *in* that share, where the command, the environment and the working
    directory can be any size.
    """
    ctl = shlex.quote(GUEST_CONTROL_DIR)
    return (
        f"mkdir -p {ctl} 2>/dev/null; "
        f"mount -t virtiofs {CONTROL_TAG} {ctl} || exit 125; "
        f"exec {GUEST_SHELL} {ctl}/{_CONTROL_SCRIPT}"
    )


def build_guest_script(*, cmd: Sequence[str], workspace_mount: str, cwd: str) -> str:
    """Render the wrapper the guest runs once the control share is mounted.

    It mounts the workspace, applies the session environment, runs *cmd* with
    stdio wired to files in the control share, and - last - writes the status
    line that :func:`resolve_guest_exit_code` treats as authoritative. Writing
    the status last is what makes its presence mean "the command completed": a
    failed mount exits before it and is reported as a VM failure.
    """
    quoted_cmd = " ".join(shlex.quote(part) for part in cmd)
    ws = shlex.quote(workspace_mount)
    ctl = shlex.quote(GUEST_CONTROL_DIR)
    target = shlex.quote(cwd)
    # 125 is libkrun's own "init could not set up the environment" code, which
    # is exactly what a failed workspace mount is from the caller's viewpoint.
    return f"""set -u
mkdir -p {ws} 2>/dev/null
mount -t virtiofs {WORKSPACE_TAG} {ws} || exit 125
. {ctl}/{_CONTROL_ENV}
cd {target} 2>>{ctl}/{_CONTROL_STDERR}
__bns_rc=$?
if [ "$__bns_rc" -eq 0 ]; then
{quoted_cmd} <{ctl}/{_CONTROL_STDIN} >>{ctl}/{_CONTROL_STDOUT} 2>>{ctl}/{_CONTROL_STDERR}
__bns_rc=$?
fi
printf '{STATUS_PREFIX}%s\\n' "$__bns_rc" >{ctl}/{_CONTROL_STATUS}
exit 0
"""


def build_guest_env_script(env: Mapping[str, str]) -> str:
    """Render the sourced environment file for the guest wrapper.

    The session environment does not travel on the kernel command line (see
    :func:`build_bootstrap_argument`); it is sourced from the control share, so
    its size is unbounded. Names that are not valid shell identifiers are
    dropped rather than emitted as syntax that would abort the wrapper.
    """
    lines = ["# generated per exec by LibkrunMonitor; sourced by the guest wrapper"]
    for key in sorted(env):
        if not key.isidentifier() or key.startswith("__bns_"):
            continue
        lines.append(f"export {key}={shlex.quote(env[key])}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# LibkrunMonitor
# ---------------------------------------------------------------------------


class LibkrunMonitor:
    """Real microVM monitor driving libkrun guests.

    Construction is cheap and never boots a VM. :meth:`preflight` reports every
    missing host precondition side-effect-free; :meth:`boot` runs it and raises
    :class:`~bernstein.core.sandbox.backends._vmmonitor.MicroVMUnavailableError`
    when anything is missing, never degrading to a weaker isolation mode.

    Args:
        root: Logical workspace root inside the guest.
        rootfs: Guest root filesystem *directory* on the host. Defaults to
            ``$BERNSTEIN_MICROVM_LIBKRUN_ROOTFS``.
        library: libkrun shared-library path. Defaults to
            ``$BERNSTEIN_MICROVM_LIBKRUN_LIB`` then well-known locations.
        launcher: Path to the signed ``krunlaunch`` binary. Defaults to
            ``$BERNSTEIN_MICROVM_LIBKRUN_LAUNCHER`` then the build cache.
        vcpus: Guest vCPU count.
        ram_mib: Guest RAM in MiB.
        shm_bytes: virtio-fs DAX window size, in bytes.
    """

    def __init__(
        self,
        *,
        root: str = "/workspace",
        rootfs: str | None = None,
        library: str | None = None,
        launcher: str | None = None,
        vcpus: int | None = None,
        ram_mib: int | None = None,
        shm_bytes: int | None = None,
    ) -> None:
        self._logical_root = root
        self._rootfs = rootfs or os.environ.get(ROOTFS_ENV)
        self._library_override = library or os.environ.get(LIBRARY_ENV)
        self._launcher = launcher_path(launcher)
        self._vcpus = vcpus if vcpus is not None else _env_int(VCPUS_ENV, _DEFAULT_VCPUS)
        self._ram_mib = ram_mib if ram_mib is not None else _env_int(RAM_MIB_ENV, _DEFAULT_RAM_MIB)
        self._shm_bytes = shm_bytes if shm_bytes is not None else _env_int(SHM_BYTES_ENV, _DEFAULT_SHM_BYTES)
        self._workspace: Path | None = None
        self._control_root: Path | None = None
        self._base_env: dict[str, str] = {}
        self._closed = False

    # -- protocol surface --------------------------------------------------

    @property
    def workdir(self) -> str:
        return self._logical_root

    # -- host preflight ----------------------------------------------------

    def preflight(self) -> list[str]:
        """Return every missing host precondition (empty == ready).

        Side-effect-free: it stats files and inspects a code signature, but
        loads no library, compiles nothing and starts no VM, so the selector
        can probe support before committing to a boot. The list is exhaustive
        rather than short-circuiting - an operator provisioning a host should
        learn about all of it in one pass.
        """
        return [
            *self._preflight_platform(),
            *self._preflight_libraries(),
            *self._preflight_launcher(),
            *self._preflight_rootfs(),
        ]

    def _preflight_platform(self) -> list[str]:
        """Hypervisor availability for this OS."""
        if sys.platform == "linux":
            if not Path("/dev/kvm").exists():
                return ["/dev/kvm not present (no hardware virtualization / KVM)"]
            if not os.access("/dev/kvm", os.R_OK | os.W_OK):
                return ["/dev/kvm is not readable+writable by this user"]
            return []
        if sys.platform == "darwin":
            machine = os.uname().machine
            if machine != "arm64":
                return [f"macOS host is {machine!r}; libkrun requires Apple Silicon (arm64)"]
            return []
        return [f"host OS is {sys.platform!r}; libkrun supports Linux and macOS/arm64"]

    def _preflight_libraries(self) -> list[str]:
        """libkrun itself, and the guest kernel it dlopens at start time."""
        missing: list[str] = []
        if find_libkrun(self._library_override) is None:
            hint = "" if self._library_override else f" (install libkrun, or set ${LIBRARY_ENV})"
            missing.append(f"libkrun shared library not found{hint}")
        if find_libkrunfw(self._library_override) is None:
            missing.append(
                "libkrunfw shared library not found (it carries the guest kernel; libkrun loads it when the VM starts)"
            )
        return missing

    def _preflight_launcher(self) -> list[str]:
        """The signed launcher binary that hosts one VM per exec."""
        missing: list[str] = []
        launcher = self._launcher
        if not launcher.is_file():
            return [
                f"libkrun launcher binary missing at {str(launcher)!r} "
                f"(build it with `bernstein sandbox microvm-launcher`, or set ${LAUNCHER_ENV})"
            ]
        if not os.access(launcher, os.X_OK):
            missing.append(f"libkrun launcher {str(launcher)!r} is not executable")
        if sys.platform == "darwin":
            missing.extend(self._preflight_signature(launcher))
        return missing

    def _preflight_signature(self, launcher: Path) -> list[str]:
        """macOS only: Hypervisor.framework needs an entitled executable image.

        Checked here rather than left to boot because the failure it prevents
        is opaque - ``hv_vm_create`` returns a bare ``EINVAL`` that surfaces as
        ``Internal(Vm(VmSetup(VmCreate)))``.
        """
        found = launcher_entitlements(launcher)
        if found is None:
            return [f"cannot verify the code signature of {str(launcher)!r} (codesign unavailable)"]
        required = {HYPERVISOR_ENTITLEMENT, LIBRARY_VALIDATION_ENTITLEMENT}
        absent = sorted(required - found)
        if absent:
            return [
                f"libkrun launcher {str(launcher)!r} is not code-signed with "
                f"{', '.join(absent)} (rebuild it with `bernstein sandbox microvm-launcher`)"
            ]
        return []

    def _preflight_rootfs(self) -> list[str]:
        """The operator-supplied guest root filesystem."""
        if not self._rootfs:
            return [f"guest root filesystem directory not configured (set ${ROOTFS_ENV})"]
        root = Path(self._rootfs)
        if not root.is_dir():
            return [f"guest root filesystem {self._rootfs!r} is not a directory (set ${ROOTFS_ENV})"]
        # lexists, not exists: in a real rootfs /bin/sh is usually a symlink to
        # an absolute in-guest path (busybox), which resolves inside the guest
        # and dangles on the host. Following it here would reject every rootfs
        # built the normal way.
        if not os.path.lexists(root / GUEST_SHELL.lstrip("/")):
            return [f"guest root filesystem {self._rootfs!r} has no {GUEST_SHELL}"]
        return []

    def _require_available(self) -> None:
        missing = self.preflight()
        if missing:
            detail = "; ".join(missing)
            msg = f"MicroVM (libkrun) unavailable on this host: {detail}"
            raise MicroVMUnavailableError(msg)

    # -- lifecycle ---------------------------------------------------------

    async def boot(self, *, base_env: Mapping[str, str]) -> None:
        """Validate the host and prepare an empty workspace root.

        No VM is started here. ``krun_start_enter`` owns a whole process and
        every byte of session state lives in the shared workspace rather than
        in VM memory, so a guest is booted per :meth:`exec` instead of being
        held open across the session.

        Raises:
            MicroVMUnavailableError: When any host precondition is missing.
                The backend refuses rather than degrading isolation.
        """
        self._require_available()
        self._base_env = dict(base_env)
        # Resolve symlinks up front (macOS /var -> /private/var) so the
        # containment check in _resolve compares like with like, and so the
        # path handed to virtio-fs is the real one.
        if self._workspace is None:
            self._workspace = Path(tempfile.mkdtemp(prefix="bernstein-microvm-krun-")).resolve()
        if self._control_root is None:
            self._control_root = Path(tempfile.mkdtemp(prefix="bernstein-microvm-krun-ctl-")).resolve()
        self._workspace.mkdir(parents=True, exist_ok=True)
        self._closed = False

    async def shutdown(self) -> None:
        """Tear the session down. Idempotent."""
        if self._closed:
            return
        self._closed = True
        for directory in (self._workspace, self._control_root):
            if directory is not None:
                shutil.rmtree(directory, ignore_errors=True)

    # -- filesystem surface (plain host I/O over the shared workspace) ------

    def _require_booted(self) -> Path:
        return self._require_dirs()[0]

    def _require_dirs(self) -> tuple[Path, Path]:
        """Return (workspace, control root) for a live session, or refuse."""
        if self._workspace is None or self._control_root is None or self._closed:
            msg = "microVM (libkrun) session is not booted"
            raise MicroVMUnavailableError(msg)
        return self._workspace, self._control_root

    def _resolve(self, path: str) -> Path:
        """Map a guest path to a host path pinned under the shared workspace."""
        workspace = self._require_booted()
        rel = path
        if Path(path).is_absolute():
            rel = os.path.relpath(path, self._logical_root)
        target = (workspace / rel).resolve()
        if target != workspace and workspace not in target.parents:
            msg = f"Path {path!r} escapes the guest workspace"
            raise ValueError(msg)
        return target

    async def read_file(self, path: str) -> bytes:
        target = self._resolve(path)
        if not target.exists():
            msg = f"No such file in guest: {path}"
            raise FileNotFoundError(msg)
        return target.read_bytes()

    async def write_file(self, path: str, data: bytes, *, mode: int = 0o644) -> None:
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        with contextlib.suppress(OSError):
            target.chmod(mode)

    async def ls(self, path: str) -> list[str]:
        target = self._resolve(path)
        if not target.is_dir():
            msg = f"Not a directory in guest: {path}"
            raise NotADirectoryError(msg)
        return sorted(p.name for p in target.iterdir())

    async def freeze_image(self) -> bytes:
        """Canonical workspace image of the shared directory.

        Reuses :func:`canonical_workspace_image` unchanged, so the bytes - and
        therefore the content address - are identical to the ones
        ``FakeMonitor`` produces for the same tree.
        """
        return canonical_workspace_image(self._require_booted())

    async def restore_image(self, image: bytes) -> None:
        workspace = self._require_booted()
        workspace.mkdir(parents=True, exist_ok=True)
        extract_workspace_image(image, workspace)

    # -- exec --------------------------------------------------------------

    async def exec(
        self,
        cmd: list[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: int | None = None,
        stdin: bytes | None = None,
    ) -> ExecResult:
        """Boot a guest, run *cmd* in it, and capture its result.

        Raises:
            MicroVMUnavailableError: When the VM could not run the command to
                completion. Distinct from a guest command that merely exited
                non-zero - see :func:`resolve_guest_exit_code`.
            TimeoutError: When *timeout* elapsed; the VM process is killed.
        """
        workspace, control_root = self._require_dirs()
        with self._exec_control_dir(control_root) as control:
            self._write_control_files(control, cmd=cmd, cwd=cwd, env=env, stdin=stdin)
            loop = asyncio.get_running_loop()
            start = loop.time()
            launcher_rc, launcher_stderr = await self._run_launcher(
                workspace=workspace,
                control=control,
                timeout=timeout,
                cmd=cmd,
            )
            duration = loop.time() - start
            exit_code = resolve_guest_exit_code(
                launcher_exit_code=launcher_rc,
                status_raw=_read_text(control / _CONTROL_STATUS),
                diagnostics=_diagnostics(launcher_stderr, control / _CONTROL_CONSOLE),
            )
            return ExecResult(
                exit_code=exit_code,
                stdout=_read_bytes(control / _CONTROL_STDOUT),
                stderr=_read_bytes(control / _CONTROL_STDERR),
                duration_seconds=duration,
            )

    @contextlib.contextmanager
    def _exec_control_dir(self, control_root: Path) -> Iterator[Path]:
        """A private control directory for one exec, removed afterwards.

        Per-exec rather than per-session so two concurrent ``exec`` calls on
        one session cannot read each other's status file - which would let one
        command's exit code be reported as another's.
        """
        control = control_root / uuid.uuid4().hex
        control.mkdir(parents=True)
        try:
            yield control
        finally:
            shutil.rmtree(control, ignore_errors=True)

    def _write_control_files(
        self,
        control: Path,
        *,
        cmd: list[str],
        cwd: str | None,
        env: Mapping[str, str] | None,
        stdin: bytes | None,
    ) -> None:
        """Lay out the control share the guest wrapper reads and writes."""
        run_env = dict(self._base_env)
        if env:
            run_env.update(env)
        (control / _CONTROL_STDIN).write_bytes(stdin or b"")
        (control / _CONTROL_STDOUT).write_bytes(b"")
        (control / _CONTROL_STDERR).write_bytes(b"")
        (control / _CONTROL_ENV).write_text(build_guest_env_script(run_env), encoding="utf-8")
        (control / _CONTROL_SCRIPT).write_text(
            build_guest_script(
                cmd=cmd,
                workspace_mount=self._logical_root,
                cwd=self._guest_cwd(cwd),
            ),
            encoding="utf-8",
        )

    def _guest_cwd(self, cwd: str | None) -> str:
        """Absolute guest path for *cwd* (relative paths hang off the root)."""
        if not cwd:
            return self._logical_root
        if posixpath.isabs(cwd):
            return cwd
        return posixpath.join(self._logical_root, cwd)

    def _launcher_env(self, *, workspace: Path, control: Path) -> dict[str, str]:
        """Environment for the launcher process (never reaches the guest)."""
        return {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "KRUNLAUNCH_VCPUS": str(self._vcpus),
            "KRUNLAUNCH_RAM_MIB": str(self._ram_mib),
            "KRUNLAUNCH_SHM_BYTES": str(self._shm_bytes),
            "KRUNLAUNCH_CONSOLE": str(control / _CONTROL_CONSOLE),
            # The launcher reads KRUNLAUNCH_SHARE_<n> until the first gap.
            "KRUNLAUNCH_SHARE_0": f"{WORKSPACE_TAG}={workspace}",
            "KRUNLAUNCH_SHARE_1": f"{CONTROL_TAG}={control}",
        }

    async def _run_launcher(
        self,
        *,
        workspace: Path,
        control: Path,
        timeout: int | None,
        cmd: list[str],
    ) -> tuple[int, str]:
        """Spawn one VM and wait for it, honouring *timeout*.

        Returns:
            The launcher's exit code and its stderr (diagnostics only - the
            guest's own streams come from the control share).
        """
        if self._rootfs is None:  # pragma: no cover - boot() already refused
            msg = "MicroVM (libkrun) unavailable on this host: guest rootfs not configured"
            raise MicroVMUnavailableError(msg)
        proc = await asyncio.create_subprocess_exec(
            str(self._launcher),
            self._rootfs,
            "/",
            GUEST_SHELL,
            "-c",
            build_bootstrap_argument(),
            env=self._launcher_env(workspace=workspace, control=control),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError as exc:
            await _kill(proc)
            msg = f"Command timed out after {timeout}s: {cmd!r}"
            raise TimeoutError(msg) from exc
        except asyncio.CancelledError:
            # A cancelled fork-race branch must not leave a VM running against
            # a workspace the caller is about to delete.
            await _kill(proc)
            raise
        return (
            proc.returncode if proc.returncode is not None else -1,
            stderr.decode("utf-8", "replace"),
        )


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


async def _kill(proc: asyncio.subprocess.Process) -> None:
    """Kill and reap a VM process, so an abandoned guest outlives nothing.

    A wedged or abandoned guest keeps its virtio-fs shares - and therefore this
    session's workspace - alive past ``shutdown()``.
    """
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    with contextlib.suppress(asyncio.CancelledError):
        await proc.wait()


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError:
        return b""


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _diagnostics(launcher_stderr: str, console: Path) -> str:
    """Tail of the launcher's stderr plus the guest console, for error messages."""
    parts = [launcher_stderr.strip(), (_read_text(console) or "").strip()]
    joined = " | ".join(part for part in parts if part)
    return joined[-_DIAGNOSTIC_TAIL_CHARS:]


__all__ = [
    "CONTROL_TAG",
    "GUEST_CONTROL_DIR",
    "GUEST_SHELL",
    "HYPERVISOR_ENTITLEMENT",
    "LAUNCHER_ENV",
    "LAUNCHER_NAME",
    "LIBKRUN_RESERVED_EXIT_CODES",
    "LIBRARY_ENV",
    "LIBRARY_VALIDATION_ENTITLEMENT",
    "RAM_MIB_ENV",
    "ROOTFS_ENV",
    "SHM_BYTES_ENV",
    "STATUS_PREFIX",
    "VCPUS_ENV",
    "WORKSPACE_TAG",
    "LibkrunMonitor",
    "build_bootstrap_argument",
    "build_guest_env_script",
    "build_guest_script",
    "build_launcher",
    "default_launcher_path",
    "find_libkrun",
    "find_libkrunfw",
    "launcher_entitlements",
    "launcher_path",
    "launcher_source_dir",
    "libkrun_library_dir",
    "parse_guest_status",
    "resolve_guest_exit_code",
]
