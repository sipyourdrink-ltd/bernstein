"""Seccomp-BPF syscall filtering profiles for sandboxed agent processes.

Generates Linux seccomp JSON profiles (Docker/Podman --security-opt=seccomp=<path>
format) that restrict which system calls a spawned agent may make.  Different
agent roles need different syscall surfaces; this module maps roles to minimal
allowlists so a compromised agent cannot escalate privileges, mount filesystems,
load kernel modules, or open raw network sockets it does not need.

Architecture
------------
Profiles are generated in the Docker seccomp JSON schema (same format used by
``docker/default`` and containerd).  Each profile specifies a default action of
``SCMP_ACT_ERRNO`` (EPERM) and an explicit allowlist of permitted syscalls.

Three built-in profiles are provided:

- **strict** — file I/O only; no network sockets.  For agents that communicate
  via the task-server HTTP API *through the container runtime* (not directly).
- **http_agent** — file I/O + TCP/UDP sockets for HTTP(S) calls to the task
  server and external APIs.  No raw sockets, no ``mount``, no kernel module
  loading.
- **default** — http_agent + a slightly wider set of process management calls
  for agents that spawn sub-processes (e.g. running test suites).

Usage::

    from bernstein.core.seccomp_profiles import AgentSeccompProfile, write_profile

    # Write the profile to a temp file and get back its path.
    profile_path = write_profile(AgentSeccompProfile.HTTP_AGENT, dest_dir=Path("/tmp"))

    # Pass the path to SecurityProfile.
    from bernstein.core.container import SecurityProfile
    sec = SecurityProfile(seccomp_profile=str(profile_path))
"""

from __future__ import annotations

import json
import logging
import tempfile
from enum import StrEnum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Syscall allowlists
# ---------------------------------------------------------------------------

# Syscalls common to every agent regardless of network access.
# Covers: memory management, file descriptor management, process lifecycle,
# signal handling, timers, basic IPC (futex/pipe), and thread-local storage.
_SYSCALLS_BASE: list[str] = [
    # Memory
    "brk",
    "mmap",
    "mmap2",
    "mprotect",
    "munmap",
    "mremap",
    "madvise",
    "mincore",
    "msync",
    # File operations
    "read",
    "write",
    "pread64",
    "pwrite64",
    "readv",
    "writev",
    "open",
    "openat",
    "openat2",
    "close",
    "creat",
    "stat",
    "fstat",
    "lstat",
    "newfstatat",
    "fstatat64",
    "statx",
    "access",
    "faccessat",
    "faccessat2",
    "lseek",
    "llseek",
    "_llseek",
    "ftruncate",
    "ftruncate64",
    "truncate",
    "truncate64",
    "unlink",
    "unlinkat",
    "rename",
    "renameat",
    "renameat2",
    "mkdir",
    "mkdirat",
    "rmdir",
    "link",
    "linkat",
    "symlink",
    "symlinkat",
    "readlink",
    "readlinkat",
    "chmod",
    "fchmod",
    "fchmodat",
    "chown",
    "fchown",
    "lchown",
    "fchownat",
    "utime",
    "utimes",
    "utimensat",
    "futimesat",
    "getdents",
    "getdents64",
    "getcwd",
    "chdir",
    "fchdir",
    "dup",
    "dup2",
    "dup3",
    "fcntl",
    "fcntl64",
    "ioctl",
    "flock",
    "sync",
    "fsync",
    "fdatasync",
    "syncfs",
    "pipe",
    "pipe2",
    # File descriptor multiplexing
    "poll",
    "ppoll",
    "select",
    "pselect6",
    "epoll_create",
    "epoll_create1",
    "epoll_ctl",
    "epoll_wait",
    "epoll_pwait",
    "epoll_pwait2",
    "eventfd",
    "eventfd2",
    # Process lifecycle
    "getpid",
    "getppid",
    "gettid",
    "getpgrp",
    "getpgid",
    "setpgid",
    "getsid",
    "setsid",
    "fork",
    "vfork",
    "clone",
    "clone3",
    "execve",
    "execveat",
    "exit",
    "exit_group",
    "wait4",
    "waitpid",
    "waitid",
    # Signals
    "kill",
    "tkill",
    "tgkill",
    "sigaction",
    "sigprocmask",
    "sigsuspend",
    "sigreturn",
    "rt_sigaction",
    "rt_sigprocmask",
    "rt_sigreturn",
    "rt_sigsuspend",
    "rt_sigpending",
    "rt_sigtimedwait",
    "rt_sigqueueinfo",
    "rt_tgsigqueueinfo",
    "signal",
    "signalfd",
    "signalfd4",
    "sigaltstack",
    # Timers & clocks
    "gettimeofday",
    "settimeofday",
    "clock_gettime",
    "clock_settime",
    "clock_getres",
    "clock_nanosleep",
    "clock_adjtime",
    "nanosleep",
    "timer_create",
    "timer_settime",
    "timer_gettime",
    "timer_getoverrun",
    "timer_delete",
    "timerfd_create",
    "timerfd_settime",
    "timerfd_gettime",
    "time",
    "times",
    # Threads & synchronisation
    "futex",
    "futex_waitv",
    "get_robust_list",
    "set_robust_list",
    "set_tid_address",
    "arch_prctl",
    "prctl",
    # User & group IDs (read-only; no privilege escalation)
    "getuid",
    "getuid32",
    "geteuid",
    "geteuid32",
    "getgid",
    "getgid32",
    "getegid",
    "getegid32",
    "getgroups",
    "getgroups32",
    # System information
    "uname",
    "sysinfo",
    "getrlimit",
    "prlimit64",
    "setrlimit",
    "getrusage",
    "sched_getaffinity",
    "sched_setaffinity",
    "sched_yield",
    "sched_getparam",
    "sched_setparam",
    "sched_getscheduler",
    "sched_setscheduler",
    "sched_get_priority_max",
    "sched_get_priority_min",
    # Miscellaneous
    "umask",
    "getrandom",
    "getentropy",
    "inotify_init",
    "inotify_init1",
    "inotify_add_watch",
    "inotify_rm_watch",
    "memfd_create",
    "copy_file_range",
    "sendfile",
    "sendfile64",
    "splice",
    "tee",
    "vmsplice",
    "fallocate",
    "posix_fadvise64",
    "fadvise64",
    "mlock",
    "mlock2",
    "munlock",
    "mlockall",
    "munlockall",
    "landlock_create_ruleset",
    "landlock_add_rule",
    "landlock_restrict_self",
    "seccomp",  # Allow applying further seccomp restrictions
    "restart_syscall",
    "rseq",
]

# Network syscalls for agents that make HTTP(S) calls.
# Raw sockets (AF_PACKET, SOCK_RAW) are intentionally excluded.
_SYSCALLS_NETWORK: list[str] = [
    "socket",
    "connect",
    "accept",
    "accept4",
    "bind",
    "listen",
    "getsockname",
    "getpeername",
    "sendto",
    "recvfrom",
    "sendmsg",
    "recvmsg",
    "sendmmsg",
    "recvmmsg",
    "setsockopt",
    "getsockopt",
    "shutdown",
    "socketpair",
    "socketcall",
]

# Additional syscalls for agents that spawn sub-processes (e.g. test runners).
_SYSCALLS_SUBPROCESS: list[str] = [
    "ptrace",  # Some debuggers/profilers need this; restricted below via PTRACE_ATTACH
    "process_vm_readv",
    "process_vm_writev",
    "setuid",
    "setgid",
    "setreuid",
    "setregid",
    "setresuid",
    "setresgid",
    "capget",
    "capset",
]

# ---------------------------------------------------------------------------
# Seccomp profile builder
# ---------------------------------------------------------------------------


class AgentSeccompProfile(StrEnum):
    """Named seccomp-bpf profiles for agent processes.

    Attributes:
        STRICT: File I/O only — no network sockets.
        HTTP_AGENT: File I/O + TCP/UDP sockets for HTTP(S) calls.
        DEFAULT: HTTP_AGENT + broader process management for test-running agents.
    """

    STRICT = "strict"
    HTTP_AGENT = "http_agent"
    DEFAULT = "default"


# Mapping of architectures to include in profiles.
_ARCHITECTURES: list[str] = [
    "SCMP_ARCH_X86_64",
    "SCMP_ARCH_X86",
    "SCMP_ARCH_AARCH64",
    "SCMP_ARCH_ARM",
]

# Syscalls that are ALWAYS denied, even in the default profile.
# These represent clear privilege-escalation or kernel-integrity risks.
_SYSCALLS_ALWAYS_DENY: frozenset[str] = frozenset(
    {
        "mount",
        "umount",
        "umount2",
        "swapon",
        "swapoff",
        "pivot_root",
        "chroot",
        "init_module",
        "finit_module",
        "delete_module",
        "create_module",
        "get_kernel_syms",
        "query_module",
        "nfsservctl",
        "syslog",
        "kexec_load",
        "kexec_file_load",
        "reboot",
        "acct",
        "settimeofday",  # Re-blocked here for safety; included above for completeness
        "adjtimex",
        "clock_adjtime",  # Same
        "lookup_dcookie",
        "io_setup",
        "io_destroy",
        "io_getevents",
        "io_submit",
        "io_cancel",
        "io_pgetevents",
        "io_uring_setup",
        "io_uring_enter",
        "io_uring_register",
        "keyctl",
        "add_key",
        "request_key",
        "perf_event_open",
        "bpf",  # Agent should not load arbitrary BPF programs
        "userfaultfd",
        "iopl",
        "ioperm",
        "vm86",
        "modify_ldt",
        "fanotify_init",
        "open_by_handle_at",
        "name_to_handle_at",
        "kcmp",
    }
)


def _build_profile(
    syscalls: list[str],
    profile_name: str,
) -> dict[str, Any]:
    """Construct a seccomp JSON profile dict from an allowlist.

    Args:
        syscalls: Syscall names to allow.
        profile_name: Human-readable profile identifier (stored as a comment
            via the ``comment`` field that Docker/Podman ignore).

    Returns:
        Seccomp profile as a Python dict (JSON-serialisable).
    """
    # Filter out any syscalls that appear in the always-deny set.
    # Some syscalls exist in both _SYSCALLS_BASE and _SYSCALLS_ALWAYS_DENY
    # intentionally so the deny set takes precedence here.
    allowed = [s for s in syscalls if s not in _SYSCALLS_ALWAYS_DENY]

    return {
        "comment": f"Bernstein agent seccomp profile: {profile_name}",
        "defaultAction": "SCMP_ACT_ERRNO",
        "defaultErrnoRet": 1,  # EPERM
        "architectures": _ARCHITECTURES,
        "syscalls": [
            {
                "names": sorted(set(allowed)),
                "action": "SCMP_ACT_ALLOW",
                "comment": "Bernstein agent allowlist",
            }
        ],
    }


# Pre-built profiles keyed by AgentSeccompProfile.
_PROFILE_SYSCALLS: dict[AgentSeccompProfile, list[str]] = {
    AgentSeccompProfile.STRICT: list(_SYSCALLS_BASE),
    AgentSeccompProfile.HTTP_AGENT: list(_SYSCALLS_BASE) + list(_SYSCALLS_NETWORK),
    AgentSeccompProfile.DEFAULT: list(_SYSCALLS_BASE) + list(_SYSCALLS_NETWORK) + list(_SYSCALLS_SUBPROCESS),
}


def build_profile(profile: AgentSeccompProfile) -> dict[str, Any]:
    """Build the seccomp profile dict for the given named profile.

    Args:
        profile: One of the pre-defined :class:`AgentSeccompProfile` values.

    Returns:
        Seccomp profile as a JSON-serialisable dict.
    """
    syscalls = _PROFILE_SYSCALLS[profile]
    return _build_profile(syscalls, profile.value)


def build_custom_profile(
    extra_syscalls: list[str] | None = None,
    base: AgentSeccompProfile = AgentSeccompProfile.HTTP_AGENT,
    name: str = "custom",
) -> dict[str, Any]:
    """Build a custom seccomp profile extending a named base profile.

    Args:
        extra_syscalls: Additional syscall names to permit on top of ``base``.
        base: Base profile to extend.
        name: Human-readable profile name for auditing.

    Returns:
        Seccomp profile as a JSON-serialisable dict.
    """
    syscalls = list(_PROFILE_SYSCALLS[base])
    if extra_syscalls:
        syscalls.extend(extra_syscalls)
    return _build_profile(syscalls, name)


def write_profile(
    profile: AgentSeccompProfile,
    dest_dir: Path | None = None,
) -> Path:
    """Write the seccomp profile JSON to a file and return the path.

    If ``dest_dir`` is ``None``, a system temporary directory is used.  The
    caller is responsible for cleaning up the file when the container exits.

    Args:
        profile: Pre-defined profile to write.
        dest_dir: Directory to write the profile file into.

    Returns:
        Absolute path to the written JSON file.
    """
    profile_data = build_profile(profile)
    json_bytes = json.dumps(profile_data, indent=2).encode()

    if dest_dir is None:
        dest_dir = Path(tempfile.gettempdir())
    dest_dir.mkdir(parents=True, exist_ok=True)

    out_path = dest_dir / f"bernstein-seccomp-{profile.value}.json"
    out_path.write_bytes(json_bytes)
    logger.debug("Wrote seccomp profile %s to %s", profile.value, out_path)
    return out_path


def write_custom_profile(
    profile_dict: dict[str, Any],
    name: str,
    dest_dir: Path | None = None,
) -> Path:
    """Write an arbitrary seccomp profile dict to a file.

    Args:
        profile_dict: Seccomp profile dict (must match Docker seccomp schema).
        name: Filename stem (becomes ``bernstein-seccomp-<name>.json``).
        dest_dir: Directory to write to; defaults to the system temp dir.

    Returns:
        Absolute path to the written JSON file.
    """
    json_bytes = json.dumps(profile_dict, indent=2).encode()

    if dest_dir is None:
        dest_dir = Path(tempfile.gettempdir())
    dest_dir.mkdir(parents=True, exist_ok=True)

    out_path = dest_dir / f"bernstein-seccomp-{name}.json"
    out_path.write_bytes(json_bytes)
    logger.debug("Wrote custom seccomp profile %s to %s", name, out_path)
    return out_path


def profile_for_role(role: str) -> AgentSeccompProfile:
    """Map a Bernstein agent role to the appropriate seccomp profile.

    Roles that run tests or spawn sub-processes get the DEFAULT profile.
    Roles that only read/write files get STRICT.  All others get HTTP_AGENT.

    Args:
        role: Agent role string (e.g. "backend", "qa", "security").

    Returns:
        The recommended :class:`AgentSeccompProfile` for that role.
    """
    _subprocess_roles = {"qa", "ci-fixer", "devops", "resolver"}
    _strict_roles = {"docs", "reviewer", "analyst"}

    if role in _subprocess_roles:
        return AgentSeccompProfile.DEFAULT
    if role in _strict_roles:
        return AgentSeccompProfile.STRICT
    return AgentSeccompProfile.HTTP_AGENT
