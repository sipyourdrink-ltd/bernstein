"""Unit tests for seccomp-BPF sandbox profile generation."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import json

import pytest

from bernstein.core.security.seccomp_sandbox import (
    _BUILTIN_PROFILES,
    _KNOWN_SYSCALLS,
    _ROLE_PROFILE_MAP,
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

# ---------------------------------------------------------------------------
# SyscallAction
# ---------------------------------------------------------------------------


def test_syscall_action_is_str_enum() -> None:
    assert isinstance(SyscallAction.ALLOW, str)
    assert SyscallAction.ALLOW == "ALLOW"


def test_syscall_action_members() -> None:
    assert set(SyscallAction) == {
        SyscallAction.ALLOW,
        SyscallAction.LOG,
        SyscallAction.ERRNO,
        SyscallAction.KILL,
    }


# ---------------------------------------------------------------------------
# SyscallRule
# ---------------------------------------------------------------------------


def test_syscall_rule_frozen() -> None:
    rule = SyscallRule(syscall_name="read", action=SyscallAction.ALLOW)
    with pytest.raises(AttributeError):
        rule.syscall_name = "write"  # type: ignore[misc]


def test_syscall_rule_defaults() -> None:
    rule = SyscallRule(syscall_name="write", action=SyscallAction.ALLOW)
    assert rule.errno_value is None


def test_syscall_rule_errno_value() -> None:
    rule = SyscallRule(
        syscall_name="socket",
        action=SyscallAction.ERRNO,
        errno_value=13,
    )
    assert rule.errno_value == 13


# ---------------------------------------------------------------------------
# SeccompProfile
# ---------------------------------------------------------------------------


def test_seccomp_profile_frozen() -> None:
    profile = get_builtin_profile("strict")
    with pytest.raises(AttributeError):
        profile.name = "hacked"  # type: ignore[misc]


def test_seccomp_profile_default_arch() -> None:
    profile = SeccompProfile(
        name="test",
        description="test",
        default_action=SyscallAction.ERRNO,
        rules=(),
    )
    assert profile.arch == "SCMP_ARCH_X86_64"


def test_seccomp_profile_custom_arch() -> None:
    profile = SeccompProfile(
        name="test",
        description="test",
        default_action=SyscallAction.ERRNO,
        rules=(),
        arch="SCMP_ARCH_AARCH64",
    )
    assert profile.arch == "SCMP_ARCH_AARCH64"


# ---------------------------------------------------------------------------
# get_builtin_profile
# ---------------------------------------------------------------------------


def test_get_builtin_profile_strict() -> None:
    profile = get_builtin_profile("strict")
    assert profile.name == "strict"
    assert profile.default_action == SyscallAction.ERRNO


def test_get_builtin_profile_standard() -> None:
    profile = get_builtin_profile("standard")
    assert profile.name == "standard"
    syscall_names = {r.syscall_name for r in profile.rules}
    assert "fork" in syscall_names
    assert "execve" in syscall_names


def test_get_builtin_profile_permissive() -> None:
    profile = get_builtin_profile("permissive")
    assert profile.name == "permissive"
    syscall_names = {r.syscall_name for r in profile.rules}
    assert "ptrace" in syscall_names
    assert "capset" in syscall_names


def test_get_builtin_profile_minimal() -> None:
    profile = get_builtin_profile("minimal")
    assert profile.name == "minimal"
    assert profile.default_action == SyscallAction.KILL
    syscall_names = {r.syscall_name for r in profile.rules}
    assert "read" in syscall_names
    assert "exit" in syscall_names
    # No write in minimal
    assert "write" not in syscall_names


def test_get_builtin_profile_unknown_raises() -> None:
    with pytest.raises(KeyError, match="Unknown profile"):
        get_builtin_profile("nonexistent")


def test_all_builtin_profiles_exist() -> None:
    for name in ("strict", "standard", "permissive", "minimal"):
        profile = get_builtin_profile(name)
        assert profile.name == name


def test_strict_has_no_process_syscalls() -> None:
    profile = get_builtin_profile("strict")
    syscall_names = {r.syscall_name for r in profile.rules}
    assert "fork" not in syscall_names
    assert "execve" not in syscall_names
    assert "clone" not in syscall_names


def test_strict_has_network_syscalls() -> None:
    profile = get_builtin_profile("strict")
    syscall_names = {r.syscall_name for r in profile.rules}
    assert "socket" in syscall_names
    assert "connect" in syscall_names


# ---------------------------------------------------------------------------
# generate_seccomp_json
# ---------------------------------------------------------------------------


def test_generate_seccomp_json_valid_json() -> None:
    profile = get_builtin_profile("strict")
    result = generate_seccomp_json(profile)
    doc = json.loads(result)
    assert isinstance(doc, dict)


def test_generate_seccomp_json_has_default_action() -> None:
    profile = get_builtin_profile("strict")
    doc = json.loads(generate_seccomp_json(profile))
    assert doc["defaultAction"] == "SCMP_ACT_ERRNO"


def test_generate_seccomp_json_has_architectures() -> None:
    profile = get_builtin_profile("strict")
    doc = json.loads(generate_seccomp_json(profile))
    assert "SCMP_ARCH_X86_64" in doc["architectures"]


def test_generate_seccomp_json_has_syscalls() -> None:
    profile = get_builtin_profile("strict")
    doc = json.loads(generate_seccomp_json(profile))
    assert len(doc["syscalls"]) >= 1
    entry = doc["syscalls"][0]
    assert entry["action"] == "SCMP_ACT_ALLOW"
    assert "read" in entry["names"]


def test_generate_seccomp_json_sorted_names() -> None:
    profile = get_builtin_profile("strict")
    doc = json.loads(generate_seccomp_json(profile))
    for entry in doc["syscalls"]:
        names = entry["names"]
        assert names == sorted(names)


def test_generate_seccomp_json_no_duplicates() -> None:
    profile = get_builtin_profile("standard")
    doc = json.loads(generate_seccomp_json(profile))
    for entry in doc["syscalls"]:
        names = entry["names"]
        assert len(names) == len(set(names))


def test_generate_seccomp_json_comment_includes_name() -> None:
    profile = get_builtin_profile("permissive")
    doc = json.loads(generate_seccomp_json(profile))
    assert "permissive" in doc["comment"]


def test_generate_seccomp_json_errno_rule_includes_errno_ret() -> None:
    rules = (
        SyscallRule(
            syscall_name="socket",
            action=SyscallAction.ERRNO,
            errno_value=13,
        ),
    )
    profile = SeccompProfile(
        name="test-errno",
        description="test",
        default_action=SyscallAction.KILL,
        rules=rules,
    )
    doc = json.loads(generate_seccomp_json(profile))
    errno_entry = [e for e in doc["syscalls"] if e["action"] == "SCMP_ACT_ERRNO"]
    assert len(errno_entry) == 1
    assert errno_entry[0]["errnoRet"] == 13


def test_generate_seccomp_json_minimal_has_kill_default() -> None:
    profile = get_builtin_profile("minimal")
    doc = json.loads(generate_seccomp_json(profile))
    assert doc["defaultAction"] == "SCMP_ACT_KILL"


# ---------------------------------------------------------------------------
# validate_profile
# ---------------------------------------------------------------------------


def test_validate_builtin_profiles_clean() -> None:
    for name in _BUILTIN_PROFILES:
        issues = validate_profile(get_builtin_profile(name))
        assert issues == [], f"Profile {name!r} has issues: {issues}"


def test_validate_empty_rules() -> None:
    profile = SeccompProfile(
        name="empty",
        description="no rules",
        default_action=SyscallAction.ERRNO,
        rules=(),
    )
    issues = validate_profile(profile)
    assert any("no syscall rules" in i for i in issues)


def test_validate_unknown_syscall() -> None:
    rules = (
        SyscallRule(
            syscall_name="totally_fake_syscall",
            action=SyscallAction.ALLOW,
        ),
    )
    profile = SeccompProfile(
        name="test",
        description="test",
        default_action=SyscallAction.ERRNO,
        rules=rules,
    )
    issues = validate_profile(profile)
    assert any("Unknown syscall" in i for i in issues)


def test_validate_conflicting_rules() -> None:
    rules = (
        SyscallRule(syscall_name="read", action=SyscallAction.ALLOW),
        SyscallRule(syscall_name="read", action=SyscallAction.KILL),
    )
    profile = SeccompProfile(
        name="conflict",
        description="conflicting",
        default_action=SyscallAction.ERRNO,
        rules=rules,
    )
    issues = validate_profile(profile)
    assert any("Conflicting" in i for i in issues)


def test_validate_errno_without_value() -> None:
    rules = (
        SyscallRule(
            syscall_name="socket",
            action=SyscallAction.ERRNO,
            errno_value=None,
        ),
    )
    profile = SeccompProfile(
        name="errno-no-val",
        description="test",
        default_action=SyscallAction.ALLOW,
        rules=rules,
    )
    issues = validate_profile(profile)
    assert any("no errno_value" in i for i in issues)


def test_validate_errno_with_value_ok() -> None:
    rules = (
        SyscallRule(
            syscall_name="socket",
            action=SyscallAction.ERRNO,
            errno_value=1,
        ),
    )
    profile = SeccompProfile(
        name="errno-ok",
        description="test",
        default_action=SyscallAction.ALLOW,
        rules=rules,
    )
    issues = validate_profile(profile)
    assert not any("no errno_value" in i for i in issues)


# ---------------------------------------------------------------------------
# merge_profiles
# ---------------------------------------------------------------------------


def test_merge_profiles_requires_two() -> None:
    with pytest.raises(ValueError, match="at least 2"):
        merge_profiles(get_builtin_profile("strict"))


def test_merge_profiles_most_permissive_wins() -> None:
    strict = get_builtin_profile("strict")
    standard = get_builtin_profile("standard")
    merged = merge_profiles(strict, standard)
    syscall_names = {r.syscall_name for r in merged.rules}
    # standard has process syscalls that strict lacks
    assert "fork" in syscall_names
    assert "execve" in syscall_names
    # strict's syscalls are also present
    assert "read" in syscall_names
    assert "socket" in syscall_names


def test_merge_profiles_action_upgrade() -> None:
    """When same syscall has ERRNO in one and ALLOW in another, ALLOW wins."""
    p1 = SeccompProfile(
        name="a",
        description="a",
        default_action=SyscallAction.KILL,
        rules=(SyscallRule(syscall_name="read", action=SyscallAction.ERRNO, errno_value=1),),
    )
    p2 = SeccompProfile(
        name="b",
        description="b",
        default_action=SyscallAction.KILL,
        rules=(SyscallRule(syscall_name="read", action=SyscallAction.ALLOW),),
    )
    merged = merge_profiles(p1, p2)
    read_rule = next(r for r in merged.rules if r.syscall_name == "read")
    assert read_rule.action == SyscallAction.ALLOW


def test_merge_profiles_default_action_most_permissive() -> None:
    minimal = get_builtin_profile("minimal")  # KILL default
    strict = get_builtin_profile("strict")  # ERRNO default
    merged = merge_profiles(minimal, strict)
    assert merged.default_action == SyscallAction.ERRNO


def test_merge_profiles_name() -> None:
    a = get_builtin_profile("strict")
    b = get_builtin_profile("minimal")
    merged = merge_profiles(a, b)
    assert "strict" in merged.name
    assert "minimal" in merged.name


def test_merge_three_profiles() -> None:
    strict = get_builtin_profile("strict")
    standard = get_builtin_profile("standard")
    permissive = get_builtin_profile("permissive")
    merged = merge_profiles(strict, standard, permissive)
    syscall_names = {r.syscall_name for r in merged.rules}
    # Should have everything from all three
    assert "ptrace" in syscall_names
    assert "socket" in syscall_names
    assert "read" in syscall_names


# ---------------------------------------------------------------------------
# get_recommended_profile
# ---------------------------------------------------------------------------


def test_recommended_profile_qa_is_strict() -> None:
    profile = get_recommended_profile("qa")
    assert profile.name == "strict"


def test_recommended_profile_backend_is_standard() -> None:
    profile = get_recommended_profile("backend")
    assert profile.name == "standard"


def test_recommended_profile_security_is_permissive() -> None:
    profile = get_recommended_profile("security")
    assert profile.name == "permissive"


def test_recommended_profile_visionary_is_minimal() -> None:
    profile = get_recommended_profile("visionary")
    assert profile.name == "minimal"


def test_recommended_profile_unknown_role_defaults_to_standard() -> None:
    profile = get_recommended_profile("unknown-role-xyz")
    assert profile.name == "standard"


def test_all_template_roles_have_recommendation() -> None:
    """Every role in _ROLE_PROFILE_MAP should map to a valid profile."""
    for role, profile_name in _ROLE_PROFILE_MAP.items():
        profile = get_recommended_profile(role)
        assert profile.name == profile_name


# ---------------------------------------------------------------------------
# render_profile_summary
# ---------------------------------------------------------------------------


def test_render_profile_summary_contains_name() -> None:
    profile = get_builtin_profile("strict")
    summary = render_profile_summary(profile)
    assert "strict" in summary


def test_render_profile_summary_contains_arch() -> None:
    profile = get_builtin_profile("strict")
    summary = render_profile_summary(profile)
    assert "SCMP_ARCH_X86_64" in summary


def test_render_profile_summary_contains_action_section() -> None:
    profile = get_builtin_profile("strict")
    summary = render_profile_summary(profile)
    assert "ALLOW" in summary


def test_render_profile_summary_contains_rule_count() -> None:
    profile = get_builtin_profile("strict")
    summary = render_profile_summary(profile)
    assert str(len(profile.rules)) in summary


def test_render_profile_summary_is_markdown() -> None:
    profile = get_builtin_profile("strict")
    summary = render_profile_summary(profile)
    assert summary.startswith("## ")


def test_known_syscalls_set_is_nonempty() -> None:
    """Sanity check that the validation set has entries."""
    assert len(_KNOWN_SYSCALLS) > 100
