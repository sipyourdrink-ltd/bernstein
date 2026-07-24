"""Host-portable unit tests for :class:`LibkrunMonitor` (#2971).

These run everywhere - no hypervisor, no guest rootfs, no code signature. They
cover the four things that must hold on *every* host:

- ``preflight()`` is side-effect-free and names each missing precondition,
  including the ones specific to this adapter (the launcher binary and its
  entitlements);
- the no-silent-downgrade invariant: an unsupported host refuses at ``boot()``
  instead of degrading to a weaker isolation mode;
- the exit-code disambiguation, which is the correctness-critical half of the
  adapter (libkrun reserves 125/126/127 and a guest command may return them);
- ``freeze_image()`` produces bytes identical to ``FakeMonitor``'s for the same
  workspace tree - the issue's byte-identity acceptance criterion.

``exec()`` is exercised end to end against a *stub launcher*: a small script
that plays the part of the VM process by writing the control files a real guest
would write. That covers the whole host-side plumbing (control-directory
lifecycle, stdio capture, status resolution, timeout, cleanup) without needing a
hypervisor. The guest-side wrapper is exercised separately, by running the
generated script under the host shell with ``mount`` stubbed out.

The real boot/exec round trip lives in the opt-in host-gated integration test
``tests/integration/sandbox/test_microvm_libkrun.py``.
"""

from __future__ import annotations

import asyncio
import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from bernstein.core.sandbox.backends import _libkrun
from bernstein.core.sandbox.backends._libkrun import (
    GUEST_SHELL,
    HYPERVISOR_ENTITLEMENT,
    LAUNCHER_ENV,
    LIBKRUN_RESERVED_EXIT_CODES,
    LIBRARY_ENV,
    LIBRARY_VALIDATION_ENTITLEMENT,
    ROOTFS_ENV,
    STATUS_PREFIX,
    LibkrunMonitor,
    build_bootstrap_argument,
    build_guest_env_script,
    build_guest_script,
    default_launcher_path,
    find_libkrun,
    launcher_path,
    parse_guest_status,
    resolve_guest_exit_code,
)
from bernstein.core.sandbox.backends._vmmonitor import (
    FakeMonitor,
    MicroVMUnavailableError,
    VMMonitor,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

# ---------------------------------------------------------------------------
# Fixtures: a host that looks provisioned, minus the hypervisor
# ---------------------------------------------------------------------------


def _touch(path: Path) -> Path:
    path.write_bytes(b"")
    return path


def _make_rootfs(tmp_path: Path) -> Path:
    """A directory shaped like a guest root filesystem."""
    rootfs = tmp_path / "rootfs"
    (rootfs / "bin").mkdir(parents=True)
    (rootfs / GUEST_SHELL.lstrip("/")).write_text("#!/bin/sh\n")
    return rootfs


def _make_stub_launcher(tmp_path: Path, body: str) -> Path:
    """A stand-in for the VM process.

    A real launcher enters a microVM whose guest writes the control files; the
    stub writes them directly. That is exactly the surface ``exec()`` consumes,
    so every host-side branch stays reachable without a hypervisor.
    """
    launcher = tmp_path / "krunlaunch"
    launcher.write_text(
        f"#!{sys.executable}\n"
        + textwrap.dedent("""
            import os, sys
            control = os.environ["KRUNLAUNCH_SHARE_1"].split("=", 1)[1]
            workspace = os.environ["KRUNLAUNCH_SHARE_0"].split("=", 1)[1]
            """)
        + textwrap.dedent(body)
    )
    launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
    return launcher


_GUEST_OK = """
open(os.path.join(control, "stdout"), "w").write("out\\n")
open(os.path.join(control, "stderr"), "w").write("err\\n")
open(os.path.join(control, "status"), "w").write("bernstein-guest-status:0\\n")
sys.exit(0)
"""


def _monitor(
    tmp_path: Path,
    *,
    launcher_body: str = "sys.exit(0)\n",
    root: str = "/workspace",
) -> LibkrunMonitor:
    """A monitor whose every host precondition is satisfied by a real file."""
    return LibkrunMonitor(
        root=root,
        rootfs=str(_make_rootfs(tmp_path)),
        library=str(_touch(tmp_path / "libkrun.stub")),
        launcher=str(_make_stub_launcher(tmp_path, launcher_body)),
    )


def _stub_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub only the checks that genuinely need a hypervisor or a signature."""
    monkeypatch.setattr(LibkrunMonitor, "_preflight_platform", lambda self: [])
    monkeypatch.setattr(LibkrunMonitor, "_preflight_signature", lambda self, launcher: [])
    monkeypatch.setattr(_libkrun, "find_libkrunfw", lambda library=None: "/stub/libkrunfw")


async def _boot_stubbed(
    monitor: LibkrunMonitor,
    monkeypatch: pytest.MonkeyPatch,
    base_env: dict[str, str] | None = None,
) -> None:
    _stub_host(monkeypatch)
    await monitor.boot(base_env=base_env or {})


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_satisfies_the_vmmonitor_protocol() -> None:
    """The backend above the seam depends only on this surface."""
    monitor = LibkrunMonitor()
    assert isinstance(monitor, VMMonitor)
    for method in (
        "boot",
        "exec",
        "read_file",
        "write_file",
        "ls",
        "freeze_image",
        "restore_image",
        "shutdown",
    ):
        assert callable(getattr(monitor, method)), method
    assert monitor.workdir == "/workspace"
    assert LibkrunMonitor(root="/srv/work").workdir == "/srv/work"


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------


def test_preflight_returns_a_list_and_does_not_raise() -> None:
    """Cheap and side-effect-free so the selector can probe before booting."""
    missing = LibkrunMonitor().preflight()
    assert isinstance(missing, list)
    assert all(isinstance(entry, str) for entry in missing)


def test_preflight_creates_nothing(tmp_path: Path) -> None:
    """'Side-effect-free' is load-bearing: probing support must not touch disk."""
    monitor = LibkrunMonitor(
        rootfs=str(tmp_path / "absent-rootfs"),
        library=str(tmp_path / "absent-libkrun"),
        launcher=str(tmp_path / "absent-launcher"),
    )
    monitor.preflight()
    monitor.preflight()
    assert list(tmp_path.iterdir()) == []


def test_preflight_names_a_missing_rootfs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ROOTFS_ENV, raising=False)
    assert any(ROOTFS_ENV in entry for entry in LibkrunMonitor().preflight())


def test_preflight_names_a_rootfs_that_is_not_a_directory(tmp_path: Path) -> None:
    missing = LibkrunMonitor(rootfs=str(_touch(tmp_path / "rootfs.img"))).preflight()
    assert any("is not a directory" in entry for entry in missing)


def test_preflight_names_a_rootfs_without_a_shell(tmp_path: Path) -> None:
    empty = tmp_path / "empty-rootfs"
    empty.mkdir()
    missing = LibkrunMonitor(rootfs=str(empty)).preflight()
    assert any(GUEST_SHELL in entry for entry in missing)


def test_preflight_accepts_a_rootfs_whose_shell_is_a_dangling_symlink(tmp_path: Path) -> None:
    """A normal rootfs has ``/bin/sh -> /bin/busybox``: absolute, guest-relative.

    Resolving that link on the host makes it dangle, so a check that followed it
    would reject every rootfs built the usual way.
    """
    rootfs = tmp_path / "rootfs"
    (rootfs / "bin").mkdir(parents=True)
    (rootfs / "bin" / "sh").symlink_to("/bin/busybox")
    missing = LibkrunMonitor(rootfs=str(rootfs)).preflight()
    assert not any(GUEST_SHELL in entry for entry in missing)


def test_preflight_names_a_missing_library(tmp_path: Path) -> None:
    missing = LibkrunMonitor(library=str(tmp_path / "nope.dylib")).preflight()
    assert any("libkrun shared library not found" in entry for entry in missing)


def test_preflight_names_a_missing_launcher(tmp_path: Path) -> None:
    """A missing launcher is a reported precondition, like a missing hypervisor."""
    missing = LibkrunMonitor(launcher=str(tmp_path / "krunlaunch")).preflight()
    assert any("launcher binary missing" in entry for entry in missing)
    # The remedy must be actionable, not merely a diagnosis.
    assert any("microvm-launcher" in entry or LAUNCHER_ENV in entry for entry in missing)


def test_preflight_names_an_unsigned_launcher(tmp_path: Path) -> None:
    """macOS only: an unentitled launcher fails inside hv_vm_create, opaquely."""
    if sys.platform != "darwin":
        pytest.skip("code-signature preconditions are macOS-only")
    launcher = _make_stub_launcher(tmp_path, "sys.exit(0)\n")
    missing = LibkrunMonitor(launcher=str(launcher)).preflight()
    assert any(HYPERVISOR_ENTITLEMENT in entry for entry in missing)
    assert any(LIBRARY_VALIDATION_ENTITLEMENT in entry for entry in missing)


def test_preflight_enumerates_every_missing_precondition(tmp_path: Path) -> None:
    """It reports the whole list, not the first failure.

    An operator provisioning a host should learn everything in one pass rather
    than rediscovering the next gap after each fix.
    """
    monitor = LibkrunMonitor(
        rootfs=str(tmp_path / "absent-rootfs"),
        library=str(tmp_path / "absent-libkrun"),
        launcher=str(tmp_path / "absent-launcher"),
    )
    missing = monitor.preflight()
    assert any("libkrun shared library" in entry for entry in missing)
    assert any("launcher binary" in entry for entry in missing)
    assert any(ROOTFS_ENV in entry for entry in missing)


def test_launcher_path_prefers_the_explicit_value_then_the_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LAUNCHER_ENV, str(tmp_path / "from-env"))
    assert launcher_path(str(tmp_path / "explicit")) == tmp_path / "explicit"
    assert launcher_path() == tmp_path / "from-env"
    monkeypatch.delenv(LAUNCHER_ENV)
    assert launcher_path() == default_launcher_path()


def test_find_libkrun_rejects_a_nonexistent_explicit_path(tmp_path: Path) -> None:
    assert find_libkrun(str(tmp_path / "absent.dylib")) is None


def test_find_libkrun_accepts_an_existing_explicit_path(tmp_path: Path) -> None:
    present = _touch(tmp_path / "libkrun.dylib")
    assert find_libkrun(str(present)) == str(present)
    assert LIBRARY_ENV  # the documented override named in the refusal messages


# ---------------------------------------------------------------------------
# The no-silent-downgrade invariant
# ---------------------------------------------------------------------------


def _unprovisioned(tmp_path: Path) -> LibkrunMonitor:
    return LibkrunMonitor(
        rootfs=str(tmp_path / "absent"),
        library=str(tmp_path / "absent"),
        launcher=str(tmp_path / "absent"),
    )


@pytest.mark.asyncio
async def test_boot_refuses_on_an_unsupported_host(tmp_path: Path) -> None:
    with pytest.raises(MicroVMUnavailableError):
        await _unprovisioned(tmp_path).boot(base_env={})


@pytest.mark.asyncio
async def test_the_refusal_names_the_missing_preconditions(tmp_path: Path) -> None:
    """The error is the operator's remediation list, not just a failure."""
    with pytest.raises(MicroVMUnavailableError) as excinfo:
        await _unprovisioned(tmp_path).boot(base_env={})
    message = str(excinfo.value)
    assert "libkrun shared library" in message
    assert "launcher binary" in message
    assert ROOTFS_ENV in message


@pytest.mark.asyncio
async def test_a_refused_boot_leaves_no_usable_session(tmp_path: Path) -> None:
    """Refusal must be total: no half-open session that later reads as isolated."""
    monitor = _unprovisioned(tmp_path)
    with pytest.raises(MicroVMUnavailableError):
        await monitor.boot(base_env={})
    for coro in (
        monitor.read_file("x"),
        monitor.write_file("x", b""),
        monitor.ls("."),
        monitor.freeze_image(),
        monitor.restore_image(b""),
        monitor.exec(["true"]),
    ):
        with pytest.raises(MicroVMUnavailableError):
            await coro


@pytest.mark.asyncio
async def test_shutdown_is_idempotent_after_a_refused_boot(tmp_path: Path) -> None:
    monitor = _unprovisioned(tmp_path)
    with pytest.raises(MicroVMUnavailableError):
        await monitor.boot(base_env={})
    await monitor.shutdown()
    await monitor.shutdown()


# ---------------------------------------------------------------------------
# Exit-code disambiguation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", sorted(LIBKRUN_RESERVED_EXIT_CODES))
def test_a_guest_command_may_legitimately_return_a_reserved_code(code: int) -> None:
    """125/126/127 from the guest is a *result* when the wrapper reported it."""
    assert (
        resolve_guest_exit_code(
            launcher_exit_code=code,
            status_raw=f"{STATUS_PREFIX}{code}\n",
        )
        == code
    )


@pytest.mark.parametrize("code", sorted(LIBKRUN_RESERVED_EXIT_CODES))
def test_a_libkrun_reserved_code_without_a_status_is_a_vm_failure(code: int) -> None:
    """The same number, no status file: a VM failure, never reported as a result."""
    with pytest.raises(MicroVMUnavailableError) as excinfo:
        resolve_guest_exit_code(launcher_exit_code=code, status_raw=None)
    assert LIBKRUN_RESERVED_EXIT_CODES[code] in str(excinfo.value)


def test_reserved_codes_are_never_conflated() -> None:
    """The whole point: the two readings of one number stay distinguishable."""
    for code in LIBKRUN_RESERVED_EXIT_CODES:
        assert resolve_guest_exit_code(launcher_exit_code=code, status_raw=f"{STATUS_PREFIX}{code}") == code
        with pytest.raises(MicroVMUnavailableError):
            resolve_guest_exit_code(launcher_exit_code=code, status_raw=None)


def test_a_status_file_beats_a_disagreeing_process_exit_code() -> None:
    """The wrapper's report is authoritative; the process code is a fallback."""
    assert resolve_guest_exit_code(launcher_exit_code=0, status_raw=f"{STATUS_PREFIX}9") == 9
    assert resolve_guest_exit_code(launcher_exit_code=1, status_raw=f"{STATUS_PREFIX}0") == 0


def test_a_vm_failure_quotes_its_diagnostics() -> None:
    """The launcher's own stderr is often the only clue to a boot failure."""
    with pytest.raises(MicroVMUnavailableError) as excinfo:
        resolve_guest_exit_code(
            launcher_exit_code=125,
            status_raw=None,
            diagnostics="krunlaunch: krun_add_virtiofs3(root) failed rc=-22",
        )
    assert "krun_add_virtiofs3" in str(excinfo.value)


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "\n",
        "garbage",
        STATUS_PREFIX,
        f"{STATUS_PREFIX}abc",
        f"{STATUS_PREFIX}-1",
        f"{STATUS_PREFIX}256",
        f"{STATUS_PREFIX}1 2",
    ],
)
def test_a_malformed_status_is_not_a_result(raw: str) -> None:
    """A partially-written or foreign file must never read as an exit code."""
    assert parse_guest_status(raw) is None
    with pytest.raises(MicroVMUnavailableError):
        resolve_guest_exit_code(launcher_exit_code=0, status_raw=raw)


@pytest.mark.parametrize("code", [0, 1, 2, 42, 125, 126, 127, 128, 255])
def test_every_valid_exit_code_round_trips(code: int) -> None:
    assert parse_guest_status(f"{STATUS_PREFIX}{code}\n") == code


def test_status_parsing_tolerates_surrounding_noise() -> None:
    """Console spill into the status file must not corrupt the reading."""
    raw = f"[  0.42] random kernel line\n  {STATUS_PREFIX}17  \ntrailing\n"
    assert parse_guest_status(raw) == 17


def test_an_unknown_nonzero_exit_without_a_status_is_a_vm_failure() -> None:
    """An unexplained exit is reported as unknown, never guessed as a result."""
    with pytest.raises(MicroVMUnavailableError) as excinfo:
        resolve_guest_exit_code(launcher_exit_code=3, status_raw=None)
    assert "unknown" in str(excinfo.value)


# ---------------------------------------------------------------------------
# The guest-side program
# ---------------------------------------------------------------------------


def test_the_bootstrap_argument_stays_small_and_constant() -> None:
    """It travels on the kernel command line, which overflows fatally.

    Everything variable (the command, the environment, the working directory)
    goes into the control share instead, so this string never grows with them.
    """
    bootstrap = build_bootstrap_argument()
    assert len(bootstrap) < 256
    assert _libkrun.CONTROL_TAG in bootstrap
    assert _libkrun.GUEST_CONTROL_DIR in bootstrap
    assert bootstrap == build_bootstrap_argument()


def test_guest_script_mounts_the_workspace_and_reports_status_last() -> None:
    """Ordering is the contract: a status file means the command completed."""
    script = build_guest_script(cmd=["echo", "hi"], workspace_mount="/workspace", cwd="/workspace")
    mount_at = script.index(f"mount -t virtiofs {_libkrun.WORKSPACE_TAG}")
    status_at = script.index(STATUS_PREFIX)
    command_at = script.index("echo hi")
    assert mount_at < command_at < status_at
    assert "exit 125" in script  # a failed mount is a VM failure, not a result


def test_guest_script_quotes_hostile_arguments() -> None:
    """Command parts are data. They must not become guest shell syntax."""
    script = build_guest_script(
        cmd=["echo", "; rm -rf /", "$(id)"],
        workspace_mount="/workspace",
        cwd="/workspace",
    )
    assert "'; rm -rf /'" in script
    assert "'$(id)'" in script


def test_guest_env_script_quotes_values_and_drops_invalid_names() -> None:
    """Values are data. Sourcing the file must not execute any of them."""
    hostile = "a b'c; echo pwned"
    rendered = build_guest_env_script({"OK": hostile, "bad-name": "x", "": "y"})
    assert "bad-name" not in rendered
    proc = subprocess.run(
        ["/bin/sh", "-c", f'{rendered}\nprintf %s "$OK"'],
        capture_output=True,
        check=True,
    )
    assert proc.stdout == hostile.encode()


def test_guest_env_script_is_deterministic() -> None:
    forward = {"B": "2", "A": "1", "C": "3"}
    backward = {"C": "3", "A": "1", "B": "2"}
    assert build_guest_env_script(forward) == build_guest_env_script(backward)


def _run_wrapper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    cmd: Sequence[str],
    stdin: bytes = b"",
    env: dict[str, str] | None = None,
    mount_fails: bool = False,
) -> tuple[int, bytes, bytes, str | None]:
    """Execute the generated wrapper under the host shell.

    ``mount`` and the two guest mount points are the only guest-specific
    pieces, so stubbing ``mount`` on ``PATH`` and pointing the directories at
    real temporary ones exercises the wrapper's actual stdio/status logic
    without a hypervisor.
    """
    workspace = tmp_path / "ws"
    control = tmp_path / "ctl"
    bindir = tmp_path / "bin"
    for directory in (workspace, control, bindir):
        directory.mkdir(exist_ok=True)
    mount = bindir / "mount"
    mount.write_text("#!/bin/sh\nexit 1\n" if mount_fails else "#!/bin/sh\nexit 0\n")
    mount.chmod(0o755)

    monkeypatch.setattr(_libkrun, "GUEST_CONTROL_DIR", str(control))
    (control / "stdin").write_bytes(stdin)
    (control / "stdout").write_bytes(b"")
    (control / "stderr").write_bytes(b"")
    (control / "env.sh").write_text(build_guest_env_script(env or {}))
    script = build_guest_script(cmd=cmd, workspace_mount=str(workspace), cwd=str(workspace))
    proc = subprocess.run(
        ["/bin/sh", "-c", script],
        capture_output=True,
        env={"PATH": f"{bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}"},
        check=False,
    )
    status_file = control / "status"
    return (
        proc.returncode,
        (control / "stdout").read_bytes(),
        (control / "stderr").read_bytes(),
        status_file.read_text() if status_file.exists() else None,
    )


@pytest.mark.parametrize("code", [0, 1, 42, 125, 126, 127])
def test_wrapper_records_the_commands_own_exit_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int) -> None:
    """Including the reserved values - that is what makes them distinguishable."""
    rc, _, _, status = _run_wrapper(tmp_path, monkeypatch, cmd=["/bin/sh", "-c", f"exit {code}"])
    assert rc == 0
    assert resolve_guest_exit_code(launcher_exit_code=rc, status_raw=status) == code


def test_wrapper_separates_stdout_and_stderr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, out, err, status = _run_wrapper(tmp_path, monkeypatch, cmd=["/bin/sh", "-c", "echo O; echo E >&2"])
    assert out == b"O\n"
    assert err == b"E\n"
    assert parse_guest_status(status) == 0


def test_wrapper_delivers_stdin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, out, _, _ = _run_wrapper(tmp_path, monkeypatch, cmd=["/bin/sh", "-c", "cat"], stdin=b"fed\n")
    assert out == b"fed\n"


def test_wrapper_applies_the_session_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, out, _, _ = _run_wrapper(
        tmp_path,
        monkeypatch,
        cmd=["/bin/sh", "-c", "echo $BERNSTEIN_SESSION"],
        env={"BERNSTEIN_SESSION": "abc123"},
    )
    assert out == b"abc123\n"


def test_wrapper_does_not_interpret_arguments_as_shell_syntax(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, out, _, _ = _run_wrapper(tmp_path, monkeypatch, cmd=["echo", "a; echo b"])
    assert out == b"a; echo b\n"


def test_wrapper_reports_a_failed_mount_as_a_vm_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No status file, reserved exit code: unambiguously a VM-level failure."""
    rc, _, _, status = _run_wrapper(tmp_path, monkeypatch, cmd=["/bin/sh", "-c", "exit 0"], mount_fails=True)
    assert rc == 125
    assert status is None
    with pytest.raises(MicroVMUnavailableError):
        resolve_guest_exit_code(launcher_exit_code=rc, status_raw=status)


# ---------------------------------------------------------------------------
# The filesystem surface (plain host I/O over the shared workspace)
# ---------------------------------------------------------------------------


async def _populate(monitor: FakeMonitor | LibkrunMonitor) -> None:
    """The same workspace tree, written through the protocol surface."""
    await monitor.write_file("top.txt", b"alpha")
    await monitor.write_file("sub/nested.txt", b"beta")
    await monitor.write_file("sub/deeper/leaf.bin", b"\x00\x01\x02")
    await monitor.write_file("private.key", b"secret", mode=0o600)


@pytest.mark.asyncio
async def test_freeze_image_is_byte_identical_to_fakemonitor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The issue's acceptance criterion, and the reason to reuse the helper.

    ``freeze_image`` calls ``canonical_workspace_image`` unchanged, so the
    snapshot's content address cannot drift between monitors: the same tree
    yields the same bytes whichever one produced it.
    """
    monitor = _monitor(tmp_path)
    await _boot_stubbed(monitor, monkeypatch)
    fake = FakeMonitor()
    await fake.boot(base_env={})
    try:
        await _populate(monitor)
        await _populate(fake)
        assert await monitor.freeze_image() == await fake.freeze_image()
    finally:
        await monitor.shutdown()
        await fake.shutdown()


@pytest.mark.asyncio
async def test_freeze_and_restore_round_trip_across_monitors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An image frozen here restores identically into a FakeMonitor."""
    monitor = _monitor(tmp_path)
    await _boot_stubbed(monitor, monkeypatch)
    fake = FakeMonitor()
    await fake.boot(base_env={})
    try:
        await _populate(monitor)
        image = await monitor.freeze_image()
        await fake.restore_image(image)
        assert await fake.freeze_image() == image
        assert await fake.read_file("sub/nested.txt") == b"beta"
    finally:
        await monitor.shutdown()
        await fake.shutdown()


@pytest.mark.asyncio
async def test_restore_image_repopulates_the_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monitor = _monitor(tmp_path)
    await _boot_stubbed(monitor, monkeypatch)
    fake = FakeMonitor()
    await fake.boot(base_env={})
    try:
        await _populate(fake)
        await monitor.restore_image(await fake.freeze_image())
        assert await monitor.read_file("top.txt") == b"alpha"
        assert await monitor.ls("/workspace") == ["private.key", "sub", "top.txt"]
    finally:
        await monitor.shutdown()
        await fake.shutdown()


@pytest.mark.asyncio
async def test_file_roundtrip_through_the_shared_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monitor = _monitor(tmp_path)
    await _boot_stubbed(monitor, monkeypatch)
    try:
        await monitor.write_file("a.txt", b"one")
        await monitor.write_file("d/b.txt", b"two")
        assert await monitor.read_file("a.txt") == b"one"
        assert await monitor.read_file("/workspace/d/b.txt") == b"two"
        assert await monitor.ls("/workspace") == ["a.txt", "d"]
    finally:
        await monitor.shutdown()


@pytest.mark.asyncio
async def test_missing_file_raises_filenotfound(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monitor = _monitor(tmp_path)
    await _boot_stubbed(monitor, monkeypatch)
    try:
        with pytest.raises(FileNotFoundError):
            await monitor.read_file("absent")
    finally:
        await monitor.shutdown()


@pytest.mark.asyncio
async def test_ls_on_a_file_raises_notadirectory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monitor = _monitor(tmp_path)
    await _boot_stubbed(monitor, monkeypatch)
    try:
        await monitor.write_file("f", b"")
        with pytest.raises(NotADirectoryError):
            await monitor.ls("f")
    finally:
        await monitor.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("escape", ["../outside", "/etc/passwd", "a/../../outside"])
async def test_paths_cannot_escape_the_shared_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, escape: str
) -> None:
    """The share is a passthrough; containment is enforced on the host side."""
    monitor = _monitor(tmp_path)
    await _boot_stubbed(monitor, monkeypatch)
    try:
        with pytest.raises(ValueError, match="escapes"):
            await monitor.write_file(escape, b"x")
    finally:
        await monitor.shutdown()


@pytest.mark.asyncio
async def test_shutdown_removes_the_workspace_and_control_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monitor = _monitor(tmp_path)
    await _boot_stubbed(monitor, monkeypatch)
    await monitor.write_file("f", b"x")
    workspace = monitor._workspace
    control = monitor._control_root
    assert workspace is not None
    assert control is not None
    await monitor.shutdown()
    assert not workspace.exists()
    assert not control.exists()


@pytest.mark.asyncio
async def test_operations_refuse_after_shutdown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monitor = _monitor(tmp_path)
    await _boot_stubbed(monitor, monkeypatch)
    await monitor.shutdown()
    with pytest.raises(MicroVMUnavailableError):
        await monitor.read_file("f")


# ---------------------------------------------------------------------------
# exec(), against a stub launcher standing in for the VM process
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_returns_the_guests_streams_and_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monitor = _monitor(tmp_path, launcher_body=_GUEST_OK)
    await _boot_stubbed(monitor, monkeypatch)
    try:
        result = await monitor.exec(["echo", "hi"])
        assert result.exit_code == 0
        assert result.stdout == b"out\n"
        assert result.stderr == b"err\n"
        assert result.duration_seconds >= 0
    finally:
        await monitor.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [1, 42, 125, 126, 127])
async def test_exec_reports_a_guest_reserved_code_as_a_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    """The launcher exits with the same reserved number; the status file wins."""
    body = f"""
open(os.path.join(control, "status"), "w").write("bernstein-guest-status:{code}\\n")
sys.exit({code})
"""
    monitor = _monitor(tmp_path, launcher_body=body)
    await _boot_stubbed(monitor, monkeypatch)
    try:
        assert (await monitor.exec(["true"])).exit_code == code
    finally:
        await monitor.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("code", sorted(LIBKRUN_RESERVED_EXIT_CODES))
async def test_exec_reports_a_libkrun_reserved_code_as_a_vm_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    """Same number, no status file: the VM failed, and exec must say so."""
    body = f'sys.stderr.write("krunlaunch: boom\\n"); sys.exit({code})\n'
    monitor = _monitor(tmp_path, launcher_body=body)
    await _boot_stubbed(monitor, monkeypatch)
    try:
        with pytest.raises(MicroVMUnavailableError) as excinfo:
            await monitor.exec(["true"])
        assert LIBKRUN_RESERVED_EXIT_CODES[code] in str(excinfo.value)
        assert "krunlaunch: boom" in str(excinfo.value)
    finally:
        await monitor.shutdown()


@pytest.mark.asyncio
async def test_exec_hands_the_guest_its_stdin_command_and_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control share is the whole channel: nothing rides the command line."""
    body = """
payload = open(os.path.join(control, "stdin"), "rb").read()
script = open(os.path.join(control, "run.sh")).read()
envsh = open(os.path.join(control, "env.sh")).read()
open(os.path.join(control, "stdout"), "wb").write(payload)
open(os.path.join(control, "stderr"), "w").write(script + envsh)
open(os.path.join(control, "status"), "w").write("bernstein-guest-status:0\\n")
sys.exit(0)
"""
    monitor = _monitor(tmp_path, launcher_body=body)
    await _boot_stubbed(monitor, monkeypatch)
    try:
        result = await monitor.exec(
            ["printenv", "SESSION"],
            cwd="sub",
            env={"SESSION": "call-level"},
            stdin=b"piped",
        )
        assert result.stdout == b"piped"
        rendered = result.stderr.decode()
        assert "printenv SESSION" in rendered
        assert "export SESSION=call-level" in rendered
        assert "cd /workspace/sub" in rendered
    finally:
        await monitor.shutdown()


@pytest.mark.asyncio
async def test_exec_layers_call_env_over_base_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    body = """
open(os.path.join(control, "stdout"), "wb").write(
    open(os.path.join(control, "env.sh"), "rb").read()
)
open(os.path.join(control, "status"), "w").write("bernstein-guest-status:0\\n")
sys.exit(0)
"""
    monitor = _monitor(tmp_path, launcher_body=body)
    await _boot_stubbed(monitor, monkeypatch, {"SHARED": "base", "ONLY_BASE": "1"})
    try:
        rendered = (await monitor.exec(["true"], env={"SHARED": "call"})).stdout.decode()
        assert "export SHARED=call" in rendered
        assert "export ONLY_BASE=1" in rendered
    finally:
        await monitor.shutdown()


@pytest.mark.asyncio
async def test_exec_never_leaves_control_files_in_the_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control state in the workspace would shift the snapshot's content address."""
    monitor = _monitor(tmp_path, launcher_body=_GUEST_OK)
    await _boot_stubbed(monitor, monkeypatch)
    fake = FakeMonitor()
    await fake.boot(base_env={})
    try:
        await monitor.exec(["true"])
        await monitor.exec(["true"], stdin=b"x")
        assert await monitor.ls("/workspace") == []
        assert await monitor.freeze_image() == await fake.freeze_image()
    finally:
        await monitor.shutdown()
        await fake.shutdown()


@pytest.mark.asyncio
async def test_exec_does_not_reuse_a_previous_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each exec gets a private control directory, so no stale result leaks."""
    body = """
marker = os.path.join(control, "status")
if os.path.exists(marker):
    sys.exit(70)
open(marker, "w").write("bernstein-guest-status:5\\n")
sys.exit(0)
"""
    monitor = _monitor(tmp_path, launcher_body=body)
    await _boot_stubbed(monitor, monkeypatch)
    try:
        assert (await monitor.exec(["true"])).exit_code == 5
        assert (await monitor.exec(["true"])).exit_code == 5
    finally:
        await monitor.shutdown()


@pytest.mark.asyncio
async def test_exec_times_out_and_kills_the_vm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monitor = _monitor(tmp_path, launcher_body="import time\ntime.sleep(30)\n")
    await _boot_stubbed(monitor, monkeypatch)
    try:
        with pytest.raises(TimeoutError):
            await monitor.exec(["sleep", "30"], timeout=1)
    finally:
        await monitor.shutdown()


@pytest.mark.asyncio
async def test_exec_removes_its_control_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A per-exec directory that outlived the call would leak stdio to disk."""
    monitor = _monitor(tmp_path, launcher_body=_GUEST_OK)
    await _boot_stubbed(monitor, monkeypatch)
    try:
        await monitor.exec(["true"])
        control_root = monitor._control_root
        assert control_root is not None
        assert list(control_root.iterdir()) == []
    finally:
        await monitor.shutdown()


@pytest.mark.asyncio
async def test_cancelling_an_exec_kills_the_vm_and_cleans_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A cancelled fork-race branch must not strand a VM on the workspace.

    The guest holds the session's virtio-fs shares open, so a VM that outlives
    its cancelled call keeps writing into a workspace the caller is about to
    delete.
    """
    monitor = _monitor(tmp_path, launcher_body="import time\ntime.sleep(30)\n")
    await _boot_stubbed(monitor, monkeypatch)
    try:
        task = asyncio.create_task(monitor.exec(["sleep", "30"]))
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        control_root = monitor._control_root
        assert control_root is not None
        assert list(control_root.iterdir()) == []
    finally:
        await monitor.shutdown()


# ---------------------------------------------------------------------------
# The packaged launcher and its build step
# ---------------------------------------------------------------------------


def test_build_launcher_refuses_without_libkrun(tmp_path: Path) -> None:
    """It never silently produces a launcher that cannot run a VM."""
    with pytest.raises(MicroVMUnavailableError, match="libkrun shared library"):
        _libkrun.build_launcher(dest=tmp_path / "krunlaunch", library=str(tmp_path / "absent.dylib"))


def test_the_launcher_source_ships_with_the_package() -> None:
    """The build step compiles packaged source, not a checkout-only file."""
    source_dir = _libkrun.launcher_source_dir()
    assert (source_dir / "krunlaunch.c").is_file()
    plist = (source_dir / "krunlaunch.entitlements.plist").read_text()
    assert HYPERVISOR_ENTITLEMENT in plist
    assert LIBRARY_VALIDATION_ENTITLEMENT in plist


def test_the_launcher_shares_the_workspace_with_host_stored_permissions() -> None:
    """Guest and host must agree on a file's mode, or freezing loses it.

    Under libkrun's default virtio-fs semantics a file the guest creates 0644
    lands on the host 0600 (the real mode lives in an extended attribute), so a
    snapshot taken from the host would silently drop the executable bit off
    anything the guest built.
    """
    source = (_libkrun.launcher_source_dir() / "krunlaunch.c").read_text()
    assert "KRUN_SEMANTICS_LINUX_SIMPLIFIED 1" in source
    assert "krun_add_virtiofs4" in source


def test_the_launcher_source_passes_an_explicit_guest_environment() -> None:
    """Passing NULL makes libkrun fold the host environment into the kernel
    command line, overrun its size limit, and abort the VMM."""
    source = (_libkrun.launcher_source_dir() / "krunlaunch.c").read_text()
    assert "krun_set_exec((uint32_t)ctx, exec_path, gargv, genvp)" in source
    assert "PATH=/bin:/sbin:/usr/bin:/usr/sbin" in source
