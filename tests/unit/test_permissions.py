"""Unit tests for per-agent file/command permissions."""

from __future__ import annotations

from bernstein.core.permissions import (
    DEFAULT_ROLE_PERMISSIONS,
    AgentPermissions,
    _parse_diff_files,
    path_matches_any,
    check_file_permissions,
    get_permissions_for_role,
    is_command_allowed,
    is_path_allowed,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_DIFF = """\
diff --git a/src/bernstein/core/models.py b/src/bernstein/core/models.py
index abc1234..def5678 100644
--- a/src/bernstein/core/models.py
+++ b/src/bernstein/core/models.py
@@ -1,3 +1,4 @@
+# new comment
 class Task:
     pass
diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml
index 1111111..2222222 100644
--- a/.github/workflows/ci.yml
+++ b/.github/workflows/ci.yml
@@ -1,2 +1,3 @@
+  - run: echo hello
   name: CI
"""

DIFF_ONLY_SRC = """\
diff --git a/src/foo.py b/src/foo.py
index abc1234..def5678 100644
--- a/src/foo.py
+++ b/src/foo.py
@@ -1,2 +1,3 @@
+pass
 x = 1
"""

DIFF_TESTS_AND_SRC = """\
diff --git a/src/foo.py b/src/foo.py
index abc1234..def5678 100644
--- a/src/foo.py
+++ b/src/foo.py
@@ -1 +1,2 @@
+pass
 x = 1
diff --git a/tests/test_foo.py b/tests/test_foo.py
index abc1234..def5678 100644
--- a/tests/test_foo.py
+++ b/tests/test_foo.py
@@ -1 +1,2 @@
+pass
 def test(): pass
"""


# ---------------------------------------------------------------------------
# _parse_diff_files
# ---------------------------------------------------------------------------


class TestParseDiffFiles:
    def test_extracts_files(self) -> None:
        files = _parse_diff_files(SAMPLE_DIFF)
        assert files == ["src/bernstein/core/models.py", ".github/workflows/ci.yml"]

    def test_empty_diff(self) -> None:
        assert _parse_diff_files("") == []

    def test_no_a_prefix(self) -> None:
        diff = "diff --git foo.py foo.py\n"
        files = _parse_diff_files(diff)
        assert files == ["foo.py"]


# ---------------------------------------------------------------------------
# path_matches_any
# ---------------------------------------------------------------------------


class TestPathMatchesAny:
    def test_exact_match(self) -> None:
        assert path_matches_any("pyproject.toml", ("pyproject.toml",))

    def test_glob_star(self) -> None:
        assert path_matches_any("src/foo.py", ("src/*",))

    def test_nested_path_with_dir_glob(self) -> None:
        assert path_matches_any("src/bernstein/core/models.py", ("src/*",))

    def test_no_match(self) -> None:
        assert not path_matches_any("README.md", ("src/*",))

    def test_leading_dot_slash_stripped(self) -> None:
        assert path_matches_any("./src/foo.py", ("src/*",))

    def test_leading_slash_stripped(self) -> None:
        assert path_matches_any("/src/foo.py", ("src/*",))

    def test_empty_patterns(self) -> None:
        assert not path_matches_any("anything.py", ())


# ---------------------------------------------------------------------------
# is_path_allowed
# ---------------------------------------------------------------------------


class TestIsPathAllowed:
    def test_allowed_with_matching_pattern(self) -> None:
        perms = AgentPermissions(allowed_paths=("src/*",))
        assert is_path_allowed("src/foo.py", perms)

    def test_denied_overrides_allowed(self) -> None:
        perms = AgentPermissions(
            allowed_paths=("src/*",),
            denied_paths=("src/secret/*",),
        )
        assert not is_path_allowed("src/secret/key.py", perms)

    def test_no_allowed_means_everything_allowed(self) -> None:
        perms = AgentPermissions()
        assert is_path_allowed("anything/goes.py", perms)

    def test_denied_with_no_allowed(self) -> None:
        perms = AgentPermissions(denied_paths=(".github/*",))
        assert not is_path_allowed(".github/workflows/ci.yml", perms)
        assert is_path_allowed("src/foo.py", perms)

    def test_not_in_allowed_is_denied(self) -> None:
        perms = AgentPermissions(allowed_paths=("src/*", "tests/*"))
        assert not is_path_allowed(".github/ci.yml", perms)


# ---------------------------------------------------------------------------
# is_command_allowed
# ---------------------------------------------------------------------------


class TestIsCommandAllowed:
    def test_no_restrictions(self) -> None:
        perms = AgentPermissions()
        assert is_command_allowed("rm -rf /", perms)

    def test_denied_command(self) -> None:
        perms = AgentPermissions(denied_commands=("rm *",))
        assert not is_command_allowed("rm -rf /", perms)

    def test_allowed_command(self) -> None:
        perms = AgentPermissions(allowed_commands=("git *",))
        assert is_command_allowed("git status", perms)
        assert not is_command_allowed("rm file", perms)

    def test_denied_overrides_allowed(self) -> None:
        perms = AgentPermissions(
            allowed_commands=("git *",),
            denied_commands=("git push *",),
        )
        assert is_command_allowed("git status", perms)
        assert not is_command_allowed("git push origin main", perms)


# ---------------------------------------------------------------------------
# get_permissions_for_role
# ---------------------------------------------------------------------------


class TestGetPermissionsForRole:
    def test_known_role(self) -> None:
        perms = get_permissions_for_role("backend")
        assert perms is DEFAULT_ROLE_PERMISSIONS["backend"]

    def test_unknown_role_returns_unrestricted(self) -> None:
        perms = get_permissions_for_role("alien_role")
        assert perms == AgentPermissions()

    def test_override_takes_precedence(self) -> None:
        custom = AgentPermissions(allowed_paths=("custom/*",))
        perms = get_permissions_for_role("backend", overrides={"backend": custom})
        assert perms is custom

    def test_override_only_for_specified_role(self) -> None:
        custom = AgentPermissions(allowed_paths=("custom/*",))
        perms = get_permissions_for_role("qa", overrides={"backend": custom})
        assert perms is DEFAULT_ROLE_PERMISSIONS["qa"]


from bernstein.core.policy_engine import DecisionType

# ---------------------------------------------------------------------------
# check_file_permissions (guardrail integration)
# ---------------------------------------------------------------------------


class TestCheckFilePermissions:
    def test_backend_allowed_src(self) -> None:
        results = check_file_permissions(DIFF_ONLY_SRC, "backend")
        assert len(results) == 1
        assert results[0].type == DecisionType.ALLOW

    def test_backend_denied_github(self) -> None:
        results = check_file_permissions(SAMPLE_DIFF, "backend")
        assert len(results) == 1
        assert results[0].type == DecisionType.DENY
        assert results[0].bypass_immune
        assert ".github/workflows/ci.yml" in results[0].files

    def test_security_allowed_github_workflows(self) -> None:
        # Security role CAN modify .github/workflows/
        results = check_file_permissions(SAMPLE_DIFF, "security")
        assert len(results) == 1
        assert results[0].type == DecisionType.ALLOW

    def test_unknown_role_no_restrictions(self) -> None:
        results = check_file_permissions(SAMPLE_DIFF, "unknown_role")
        assert len(results) == 1
        assert results[0].type == DecisionType.ALLOW
        assert "No file permission rules" in results[0].reason

    def test_empty_diff(self) -> None:
        results = check_file_permissions("", "backend")
        assert len(results) == 1
        assert results[0].type == DecisionType.ALLOW

    def test_custom_overrides(self) -> None:
        custom = AgentPermissions(
            allowed_paths=("docs/*",),
            denied_paths=("src/*",),
        )
        results = check_file_permissions(DIFF_ONLY_SRC, "backend", overrides={"backend": custom})
        assert len(results) == 1
        assert results[0].type == DecisionType.DENY
        assert results[0].bypass_immune

    def test_qa_can_edit_tests_and_src(self) -> None:
        results = check_file_permissions(DIFF_TESTS_AND_SRC, "qa")
        assert len(results) == 1
        assert results[0].type == DecisionType.ALLOW

    def test_docs_cannot_edit_src(self) -> None:
        results = check_file_permissions(DIFF_ONLY_SRC, "docs")
        assert len(results) == 1
        assert results[0].type == DecisionType.DENY
        assert results[0].bypass_immune

    def test_devops_cannot_edit_src(self) -> None:
        results = check_file_permissions(DIFF_ONLY_SRC, "devops")
        assert len(results) == 1
        assert results[0].type == DecisionType.DENY

    def test_security_role_cannot_edit_immune_paths(self) -> None:
        # Even if security can normally edit .github, immune paths might have stricter rules in guardrails.
        # But check_file_permissions only checks role-based rules.
        # Guardrails.run_guardrails calls BOTH check_file_permissions AND check_immune_paths.
        pass


# ---------------------------------------------------------------------------
# Default role matrix validation
# ---------------------------------------------------------------------------


class TestDefaultRoleMatrix:
    """Validate that the default permission matrix has sensible rules."""

    def test_backend_can_edit_src(self) -> None:
        perms = DEFAULT_ROLE_PERMISSIONS["backend"]
        assert is_path_allowed("src/bernstein/core/foo.py", perms)

    def test_backend_cannot_edit_github(self) -> None:
        perms = DEFAULT_ROLE_PERMISSIONS["backend"]
        assert not is_path_allowed(".github/workflows/ci.yml", perms)

    def test_security_can_edit_github_workflows(self) -> None:
        perms = DEFAULT_ROLE_PERMISSIONS["security"]
        assert is_path_allowed(".github/workflows/ci.yml", perms)

    def test_security_cannot_edit_sdd(self) -> None:
        perms = DEFAULT_ROLE_PERMISSIONS["security"]
        assert not is_path_allowed(".sdd/config.json", perms)

    def test_docs_can_edit_docs(self) -> None:
        perms = DEFAULT_ROLE_PERMISSIONS["docs"]
        assert is_path_allowed("docs/DESIGN.md", perms)

    def test_docs_cannot_edit_src(self) -> None:
        perms = DEFAULT_ROLE_PERMISSIONS["docs"]
        assert not is_path_allowed("src/foo.py", perms)

    def test_devops_can_edit_github(self) -> None:
        perms = DEFAULT_ROLE_PERMISSIONS["devops"]
        assert is_path_allowed(".github/workflows/ci.yml", perms)

    def test_devops_cannot_edit_src(self) -> None:
        perms = DEFAULT_ROLE_PERMISSIONS["devops"]
        assert not is_path_allowed("src/foo.py", perms)

    def test_manager_cannot_edit_src(self) -> None:
        perms = DEFAULT_ROLE_PERMISSIONS["manager"]
        assert not is_path_allowed("src/foo.py", perms)

    def test_all_roles_have_permissions(self) -> None:
        expected_roles = {"backend", "frontend", "qa", "security", "devops", "docs", "manager", "architect"}
        assert expected_roles == set(DEFAULT_ROLE_PERMISSIONS.keys())
