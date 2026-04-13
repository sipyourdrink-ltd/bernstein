"""Agent sandboxing with seccomp-BPF syscall filtering profiles.

Provides composable, frozen-dataclass-based seccomp profiles that generate
Docker/OCI ``seccomp.json`` configuration.  Unlike :mod:`seccomp_profiles`
(which uses flat allowlists), this module gives per-syscall control over the
action taken (allow, log, return-errno, kill) and supports profile merging,
validation, and role-based recommendation.

This module generates JSON configuration only -- it never invokes actual
seccomp syscalls.

Usage::

    from bernstein.core.security.seccomp_sandbox import (
        SeccompProfile,
        SyscallAction,
        SyscallRule,
        generate_seccomp_json,
        get_builtin_profile,
        get_recommended_profile,
        merge_profiles,
        render_profile_summary,
        validate_profile,
    )

    profile = get_builtin_profile("strict")
    json_cfg = generate_seccomp_json(profile)
    issues = validate_profile(profile)
    summary = render_profile_summary(profile)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Well-known Linux syscall names (subset used for validation)
# ---------------------------------------------------------------------------

_KNOWN_SYSCALLS: frozenset[str] = frozenset(
    {
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
        "mlock",
        "mlock2",
        "munlock",
        "mlockall",
        "munlockall",
        "memfd_create",
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
        "copy_file_range",
        "sendfile",
        "sendfile64",
        "splice",
        "tee",
        "vmsplice",
        "fallocate",
        "posix_fadvise64",
        "fadvise64",
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
        # Network
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
        "clock_gettime",
        "clock_getres",
        "clock_nanosleep",
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
        # User & group IDs
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
        "setuid",
        "setgid",
        "setreuid",
        "setregid",
        "setresuid",
        "setresgid",
        "capget",
        "capset",
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
        "landlock_create_ruleset",
        "landlock_add_rule",
        "landlock_restrict_self",
        "seccomp",
        "restart_syscall",
        "rseq",
        "ptrace",
        "process_vm_readv",
        "process_vm_writev",
        # Privilege / kernel (typically denied)
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
        "reboot",
        "kexec_load",
        "kexec_file_load",
        "bpf",
        "perf_event_open",
        "userfaultfd",
        "keyctl",
        "add_key",
        "request_key",
    }
)


# ---------------------------------------------------------------------------
# Enums & data models
# ---------------------------------------------------------------------------


class SyscallAction(StrEnum):
    """Action to take when a syscall is intercepted by seccomp-BPF.

    Attributes:
        ALLOW: Permit the syscall.
        LOG: Permit but log the invocation.
        ERRNO: Deny and return an errno to the caller.
        KILL: Terminate the process immediately.
    """

    ALLOW = "ALLOW"
    LOG = "LOG"
    ERRNO = "ERRNO"
    KILL = "KILL"


# Map our action enum to OCI/Docker seccomp JSON action strings.
_ACTION_TO_OCI: dict[SyscallAction, str] = {
    SyscallAction.ALLOW: "SCMP_ACT_ALLOW",
    SyscallAction.LOG: "SCMP_ACT_LOG",
    SyscallAction.ERRNO: "SCMP_ACT_ERRNO",
    SyscallAction.KILL: "SCMP_ACT_KILL",
}


@dataclass(frozen=True)
class SyscallRule:
    """A single seccomp rule binding a syscall name to an action.

    Attributes:
        syscall_name: Linux syscall name (e.g. ``"read"``, ``"socket"``).
        action: What to do when this syscall is invoked.
        errno_value: errno to return when ``action`` is ``ERRNO``.
            Ignored for other actions.
    """

    syscall_name: str
    action: SyscallAction
    errno_value: int | None = None


@dataclass(frozen=True)
class SeccompProfile:
    """A complete seccomp-BPF profile for agent sandboxing.

    Attributes:
        name: Short identifier (e.g. ``"strict"``, ``"standard"``).
        description: Human-readable explanation of the profile's purpose.
        default_action: Action for syscalls not covered by any rule.
        rules: Per-syscall overrides.
        arch: Target CPU architecture (OCI constant).
    """

    name: str
    description: str
    default_action: SyscallAction
    rules: tuple[SyscallRule, ...]
    arch: str = "SCMP_ARCH_X86_64"


# ---------------------------------------------------------------------------
# Built-in syscall sets
# ---------------------------------------------------------------------------

_SYSCALLS_READ_WRITE: tuple[str, ...] = (
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
    "stat",
    "fstat",
    "lstat",
    "newfstatat",
    "statx",
    "access",
    "faccessat",
    "faccessat2",
    "lseek",
    "getdents",
    "getdents64",
    "getcwd",
    "dup",
    "dup2",
    "dup3",
    "fcntl",
    "fcntl64",
    "flock",
    "fsync",
    "fdatasync",
    "pipe",
    "pipe2",
    "poll",
    "ppoll",
    "select",
    "pselect6",
    "epoll_create",
    "epoll_create1",
    "epoll_ctl",
    "epoll_wait",
    "epoll_pwait",
    "eventfd",
    "eventfd2",
    # Memory management (needed by practically every process)
    "brk",
    "mmap",
    "mmap2",
    "mprotect",
    "munmap",
    "mremap",
    "madvise",
    # Threads & sync
    "futex",
    "futex_waitv",
    "set_tid_address",
    "set_robust_list",
    "get_robust_list",
    "arch_prctl",
    "prctl",
    # Identity (read-only)
    "getpid",
    "getppid",
    "gettid",
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
    # Clocks (read-only)
    "gettimeofday",
    "clock_gettime",
    "clock_getres",
    "nanosleep",
    "clock_nanosleep",
    # System info
    "uname",
    "sysinfo",
    "getrlimit",
    "prlimit64",
    "getrusage",
    # Misc
    "umask",
    "getrandom",
    "getentropy",
    "restart_syscall",
    "rseq",
)

_SYSCALLS_NETWORK: tuple[str, ...] = (
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
)

_SYSCALLS_PROCESS: tuple[str, ...] = (
    "fork",
    "vfork",
    "clone",
    "clone3",
    "execve",
    "execveat",
    "wait4",
    "waitpid",
    "waitid",
    "kill",
    "tkill",
    "tgkill",
    "setpgid",
    "getpgrp",
    "getpgid",
    "getsid",
    "setsid",
    "sigaction",
    "sigprocmask",
    "rt_sigaction",
    "rt_sigprocmask",
    "rt_sigreturn",
    "sigaltstack",
)

_SYSCALLS_EXTENDED: tuple[str, ...] = (
    # File manipulation
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
    "chdir",
    "fchdir",
    "ftruncate",
    "ftruncate64",
    "truncate",
    "truncate64",
    "creat",
    "utime",
    "utimes",
    "utimensat",
    "ioctl",
    "sync",
    "syncfs",
    "copy_file_range",
    "sendfile",
    "sendfile64",
    "splice",
    "tee",
    "fallocate",
    # Timers
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
    # Signals (full set)
    "sigsuspend",
    "sigreturn",
    "rt_sigsuspend",
    "rt_sigpending",
    "rt_sigtimedwait",
    "rt_sigqueueinfo",
    "rt_tgsigqueueinfo",
    "signal",
    "signalfd",
    "signalfd4",
    # Inotify
    "inotify_init",
    "inotify_init1",
    "inotify_add_watch",
    "inotify_rm_watch",
    # Memory extended
    "mincore",
    "msync",
    "mlock",
    "mlock2",
    "munlock",
    "mlockall",
    "munlockall",
    "memfd_create",
    # Scheduling
    "sched_getaffinity",
    "sched_setaffinity",
    "sched_yield",
    "sched_getparam",
    "sched_setparam",
    "sched_getscheduler",
    "sched_setscheduler",
    "sched_get_priority_max",
    "sched_get_priority_min",
    "setrlimit",
    # Capabilities / ids
    "setuid",
    "setgid",
    "setreuid",
    "setregid",
    "setresuid",
    "setresgid",
    "capget",
    "capset",
    # Sandbox / landlock
    "landlock_create_ruleset",
    "landlock_add_rule",
    "landlock_restrict_self",
    "seccomp",
    # Debugging
    "ptrace",
    "process_vm_readv",
    "process_vm_writev",
)

# Minimal: read-only I/O + exit.
_SYSCALLS_MINIMAL: tuple[str, ...] = (
    "read",
    "pread64",
    "readv",
    "open",
    "openat",
    "close",
    "stat",
    "fstat",
    "lstat",
    "newfstatat",
    "statx",
    "access",
    "faccessat",
    "lseek",
    "getdents",
    "getdents64",
    "getcwd",
    "dup",
    "dup2",
    "fcntl",
    "pipe",
    "pipe2",
    "poll",
    "epoll_create1",
    "epoll_ctl",
    "epoll_wait",
    "exit",
    "exit_group",
    # Required for any process to function
    "brk",
    "mmap",
    "mprotect",
    "munmap",
    "futex",
    "set_tid_address",
    "arch_prctl",
    "getpid",
    "gettid",
    "getuid",
    "getgid",
    "clock_gettime",
    "gettimeofday",
    "uname",
    "getrandom",
    "restart_syscall",
    "rseq",
    "rt_sigaction",
    "rt_sigprocmask",
    "rt_sigreturn",
    "sigaltstack",
)


# ---------------------------------------------------------------------------
# Built-in profile definitions
# ---------------------------------------------------------------------------


def _make_allow_rules(syscalls: tuple[str, ...]) -> tuple[SyscallRule, ...]:
    """Create ALLOW rules for each syscall in the given tuple."""
    return tuple(SyscallRule(syscall_name=s, action=SyscallAction.ALLOW) for s in syscalls)


def _build_strict() -> SeccompProfile:
    """Build the 'strict' profile: read/write/network only."""
    syscalls = _SYSCALLS_READ_WRITE + _SYSCALLS_NETWORK
    return SeccompProfile(
        name="strict",
        description="Read/write file I/O and network sockets only. "
        "No process spawning, no filesystem mutation beyond open files.",
        default_action=SyscallAction.ERRNO,
        rules=_make_allow_rules(syscalls),
    )


def _build_standard() -> SeccompProfile:
    """Build the 'standard' profile: strict + process management."""
    syscalls = _SYSCALLS_READ_WRITE + _SYSCALLS_NETWORK + _SYSCALLS_PROCESS
    return SeccompProfile(
        name="standard",
        description="File I/O, network, and process management. "
        "Suitable for agents that run tests or spawn sub-processes.",
        default_action=SyscallAction.ERRNO,
        rules=_make_allow_rules(syscalls),
    )


def _build_permissive() -> SeccompProfile:
    """Build the 'permissive' profile: most syscalls allowed."""
    syscalls = _SYSCALLS_READ_WRITE + _SYSCALLS_NETWORK + _SYSCALLS_PROCESS + _SYSCALLS_EXTENDED
    return SeccompProfile(
        name="permissive",
        description="Broad syscall access including filesystem mutation, "
        "capabilities, debugging, and advanced scheduling. "
        "Still blocks mount/reboot/module loading.",
        default_action=SyscallAction.ERRNO,
        rules=_make_allow_rules(syscalls),
    )


def _build_minimal() -> SeccompProfile:
    """Build the 'minimal' profile: read-only I/O + exit."""
    return SeccompProfile(
        name="minimal",
        description="Read-only file access and process exit only. No writes, no network, no process spawning.",
        default_action=SyscallAction.KILL,
        rules=_make_allow_rules(_SYSCALLS_MINIMAL),
    )


_BUILTIN_PROFILES: dict[str, SeccompProfile] = {
    "strict": _build_strict(),
    "standard": _build_standard(),
    "permissive": _build_permissive(),
    "minimal": _build_minimal(),
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_builtin_profile(name: str) -> SeccompProfile:
    """Return a built-in seccomp profile by name.

    Args:
        name: One of ``"strict"``, ``"standard"``, ``"permissive"``,
            or ``"minimal"``.

    Returns:
        The corresponding frozen :class:`SeccompProfile`.

    Raises:
        KeyError: If *name* is not a known built-in profile.
    """
    try:
        return _BUILTIN_PROFILES[name]
    except KeyError:
        available = ", ".join(sorted(_BUILTIN_PROFILES))
        msg = f"Unknown profile {name!r}. Available: {available}"
        raise KeyError(msg) from None


def generate_seccomp_json(profile: SeccompProfile) -> str:
    """Generate a Docker/OCI seccomp.json string from a profile.

    The output conforms to the OCI runtime seccomp specification used by
    Docker, Podman, and containerd.

    Args:
        profile: The profile to serialise.

    Returns:
        JSON string suitable for ``--security-opt seccomp=<file>``.
    """
    # Group rules by action for compact JSON output.
    action_groups: dict[SyscallAction, list[str]] = {}
    errno_values: dict[SyscallAction, int | None] = {}

    for rule in profile.rules:
        action_groups.setdefault(rule.action, []).append(rule.syscall_name)
        if rule.errno_value is not None:
            errno_values[rule.action] = rule.errno_value

    syscalls_entries: list[dict[str, Any]] = []
    for action, names in action_groups.items():
        entry: dict[str, Any] = {
            "names": sorted(set(names)),
            "action": _ACTION_TO_OCI[action],
        }
        errno_val = errno_values.get(action)
        if action == SyscallAction.ERRNO and errno_val is not None:
            entry["errnoRet"] = errno_val
        syscalls_entries.append(entry)

    doc: dict[str, Any] = {
        "comment": f"Bernstein seccomp sandbox profile: {profile.name}",
        "defaultAction": _ACTION_TO_OCI[profile.default_action],
        "architectures": [profile.arch],
        "syscalls": syscalls_entries,
    }

    return json.dumps(doc, indent=2)


def validate_profile(profile: SeccompProfile) -> list[str]:
    """Validate a seccomp profile for correctness.

    Checks:
    - Unknown syscall names (not in the well-known set).
    - Duplicate syscall names with conflicting actions.
    - ``ERRNO`` rules missing an ``errno_value``.
    - Empty rule set.

    Args:
        profile: The profile to validate.

    Returns:
        A list of human-readable issue descriptions.  Empty means valid.
    """
    issues: list[str] = []

    if not profile.rules:
        issues.append("Profile has no syscall rules")
        return issues

    # Check for unknown syscalls.
    for rule in profile.rules:
        if rule.syscall_name not in _KNOWN_SYSCALLS:
            issues.append(f"Unknown syscall: {rule.syscall_name!r}")

    # Check for conflicting duplicates.
    seen: dict[str, SyscallAction] = {}
    for rule in profile.rules:
        prev = seen.get(rule.syscall_name)
        if prev is not None and prev != rule.action:
            issues.append(f"Conflicting rules for {rule.syscall_name!r}: {prev.value} vs {rule.action.value}")
        seen[rule.syscall_name] = rule.action

    # Check errno rules.
    for rule in profile.rules:
        if rule.action == SyscallAction.ERRNO and rule.errno_value is None:
            issues.append(f"ERRNO rule for {rule.syscall_name!r} has no errno_value")

    return issues


def merge_profiles(*profiles: SeccompProfile) -> SeccompProfile:
    """Merge multiple profiles with most-permissive-wins semantics.

    When the same syscall appears in multiple profiles with different actions,
    the most permissive action is kept.  The permissiveness order is:
    ``ALLOW > LOG > ERRNO > KILL``.

    The merged profile inherits the name and description of the first profile,
    and uses the most permissive default action across all inputs.

    Args:
        *profiles: Two or more profiles to merge.

    Returns:
        A new merged :class:`SeccompProfile`.

    Raises:
        ValueError: If fewer than two profiles are provided.
    """
    if len(profiles) < 2:
        msg = "merge_profiles requires at least 2 profiles"
        raise ValueError(msg)

    # Permissiveness ranking (higher index = more permissive).
    _PERM_RANK: dict[SyscallAction, int] = {
        SyscallAction.KILL: 0,
        SyscallAction.ERRNO: 1,
        SyscallAction.LOG: 2,
        SyscallAction.ALLOW: 3,
    }

    def _more_permissive(a: SyscallAction, b: SyscallAction) -> SyscallAction:
        return a if _PERM_RANK[a] >= _PERM_RANK[b] else b

    # Merge rules: most-permissive action wins per syscall.
    merged_rules: dict[str, SyscallRule] = {}
    for profile in profiles:
        for rule in profile.rules:
            existing = merged_rules.get(rule.syscall_name)
            if existing is None:
                merged_rules[rule.syscall_name] = rule
            else:
                winner = _more_permissive(existing.action, rule.action)
                if winner != existing.action:
                    merged_rules[rule.syscall_name] = rule
                # If actions are equal, keep existing (first-wins for ties).

    # Merge default action: most permissive wins.
    merged_default = profiles[0].default_action
    for profile in profiles[1:]:
        merged_default = _more_permissive(merged_default, profile.default_action)

    names = " + ".join(p.name for p in profiles)
    return SeccompProfile(
        name=f"merged({names})",
        description=f"Merged profile from: {names}",
        default_action=merged_default,
        rules=tuple(merged_rules[k] for k in sorted(merged_rules)),
        arch=profiles[0].arch,
    )


# Role-to-profile mapping.
_ROLE_PROFILE_MAP: dict[str, str] = {
    # Strict: roles that should only read/write and use network
    "qa": "strict",
    "reviewer": "strict",
    "analyst": "strict",
    "docs": "strict",
    # Standard: roles that need process management
    "backend": "standard",
    "frontend": "standard",
    "ci-fixer": "standard",
    "devops": "standard",
    "resolver": "standard",
    "ml-engineer": "standard",
    # Permissive: roles that need broad access
    "security": "permissive",
    "architect": "permissive",
    # Minimal: read-only roles
    "visionary": "minimal",
    "prompt-engineer": "minimal",
}


def get_recommended_profile(role: str) -> SeccompProfile:
    """Map an agent role to the recommended seccomp profile.

    Args:
        role: Bernstein agent role (e.g. ``"backend"``, ``"qa"``).

    Returns:
        The recommended :class:`SeccompProfile` for the role.
        Falls back to ``"standard"`` for unknown roles.
    """
    profile_name = _ROLE_PROFILE_MAP.get(role, "standard")
    return _BUILTIN_PROFILES[profile_name]


def render_profile_summary(profile: SeccompProfile) -> str:
    """Render a Markdown summary of a seccomp profile.

    Args:
        profile: The profile to summarise.

    Returns:
        A Markdown-formatted string describing the profile.
    """
    lines: list[str] = [
        f"## Seccomp Profile: {profile.name}",
        "",
        profile.description,
        "",
        f"- **Architecture:** `{profile.arch}`",
        f"- **Default action:** `{profile.default_action.value}`",
        f"- **Total rules:** {len(profile.rules)}",
        "",
    ]

    # Group by action for readable output.
    by_action: dict[SyscallAction, list[str]] = {}
    for rule in profile.rules:
        by_action.setdefault(rule.action, []).append(rule.syscall_name)

    for action in SyscallAction:
        names = by_action.get(action)
        if names is None:
            continue
        lines.append(f"### {action.value} ({len(names)} syscalls)")
        lines.append("")
        lines.append(", ".join(sorted(names)))
        lines.append("")

    return "\n".join(lines)
