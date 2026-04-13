"""Unit tests for seccomp-BPF profile generation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from bernstein.core.seccomp_profiles import (
    _SYSCALLS_ALWAYS_DENY,
    AgentSeccompProfile,
    build_custom_profile,
    build_profile,
    profile_for_role,
    write_custom_profile,
    write_profile,
)

# ---------------------------------------------------------------------------
# build_profile
# ---------------------------------------------------------------------------


def test_build_profile_has_deny_as_default_action() -> None:
    profile = build_profile(AgentSeccompProfile.STRICT)
    assert profile["defaultAction"] == "SCMP_ACT_ERRNO"


def test_build_profile_contains_allowed_syscalls() -> None:
    profile = build_profile(AgentSeccompProfile.STRICT)
    syscall_entry = profile["syscalls"][0]
    assert "read" in syscall_entry["names"]
    assert "write" in syscall_entry["names"]
    assert "open" in syscall_entry["names"]


def test_build_profile_strict_has_no_network_syscalls() -> None:
    profile = build_profile(AgentSeccompProfile.STRICT)
    allowed = profile["syscalls"][0]["names"]
    # socket is a network syscall not in the STRICT profile
    assert "socket" not in allowed


def test_build_profile_http_agent_includes_socket_syscalls() -> None:
    profile = build_profile(AgentSeccompProfile.HTTP_AGENT)
    allowed = profile["syscalls"][0]["names"]
    assert "socket" in allowed
    assert "connect" in allowed
    assert "sendto" in allowed


def test_build_profile_default_includes_subprocess_syscalls() -> None:
    profile = build_profile(AgentSeccompProfile.DEFAULT)
    allowed = profile["syscalls"][0]["names"]
    assert "fork" in allowed
    assert "execve" in allowed


def test_build_profile_always_deny_syscalls_absent() -> None:
    """Syscalls in _SYSCALLS_ALWAYS_DENY must not appear in any profile."""
    for profile_enum in AgentSeccompProfile:
        profile = build_profile(profile_enum)
        allowed = set(profile["syscalls"][0]["names"])
        for denied in _SYSCALLS_ALWAYS_DENY:
            assert denied not in allowed, f"Denied syscall '{denied}' found in profile '{profile_enum.value}'"


def test_build_profile_includes_architectures() -> None:
    profile = build_profile(AgentSeccompProfile.HTTP_AGENT)
    assert "SCMP_ARCH_X86_64" in profile["architectures"]
    assert "SCMP_ARCH_AARCH64" in profile["architectures"]


def test_build_profile_no_duplicate_syscalls() -> None:
    for profile_enum in AgentSeccompProfile:
        profile = build_profile(profile_enum)
        names = profile["syscalls"][0]["names"]
        assert len(names) == len(set(names)), f"Duplicate syscalls in profile '{profile_enum.value}'"


# ---------------------------------------------------------------------------
# build_custom_profile
# ---------------------------------------------------------------------------


def test_build_custom_profile_extends_base() -> None:
    custom = build_custom_profile(
        extra_syscalls=["my_syscall"],
        base=AgentSeccompProfile.STRICT,
        name="test",
    )
    allowed = custom["syscalls"][0]["names"]
    assert "my_syscall" in allowed
    assert "read" in allowed  # from STRICT base


def test_build_custom_profile_still_filters_always_deny() -> None:
    custom = build_custom_profile(
        extra_syscalls=["bpf"],  # bpf is in always-deny
        base=AgentSeccompProfile.STRICT,
    )
    allowed = custom["syscalls"][0]["names"]
    assert "bpf" not in allowed


# ---------------------------------------------------------------------------
# write_profile
# ---------------------------------------------------------------------------


def test_write_profile_creates_file(tmp_path: Path) -> None:
    out = write_profile(AgentSeccompProfile.HTTP_AGENT, dest_dir=tmp_path)
    assert out.exists()
    assert out.suffix == ".json"


def test_write_profile_content_is_valid_json(tmp_path: Path) -> None:
    out = write_profile(AgentSeccompProfile.STRICT, dest_dir=tmp_path)
    data = json.loads(out.read_text())
    assert data["defaultAction"] == "SCMP_ACT_ERRNO"


def test_write_profile_default_dir_is_tmp(monkeypatch: pytest.MonkeyPatch) -> None:
    import tempfile

    write_profile.__wrapped__ if hasattr(write_profile, "__wrapped__") else write_profile

    # Check that passing None dest_dir doesn't raise and file ends up somewhere sensible
    out = write_profile(AgentSeccompProfile.STRICT, dest_dir=None)
    assert out.exists()
    assert str(tempfile.gettempdir()) in str(out)


def test_write_custom_profile(tmp_path: Path) -> None:
    profile_dict = build_custom_profile(name="my-agent")
    out = write_custom_profile(profile_dict, name="my-agent", dest_dir=tmp_path)
    assert out.name == "bernstein-seccomp-my-agent.json"
    assert json.loads(out.read_text())["defaultAction"] == "SCMP_ACT_ERRNO"


# ---------------------------------------------------------------------------
# profile_for_role
# ---------------------------------------------------------------------------


def test_profile_for_role_qa_returns_default() -> None:
    assert profile_for_role("qa") == AgentSeccompProfile.DEFAULT


def test_profile_for_role_docs_returns_strict() -> None:
    assert profile_for_role("docs") == AgentSeccompProfile.STRICT


def test_profile_for_role_backend_returns_http_agent() -> None:
    assert profile_for_role("backend") == AgentSeccompProfile.HTTP_AGENT


def test_profile_for_role_unknown_returns_http_agent() -> None:
    assert profile_for_role("wizard") == AgentSeccompProfile.HTTP_AGENT


def test_profile_for_role_ci_fixer_returns_default() -> None:
    assert profile_for_role("ci-fixer") == AgentSeccompProfile.DEFAULT
