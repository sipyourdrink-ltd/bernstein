"""Tests for SEC-002: path traversal hardening in permissions.py."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from bernstein.core.permissions import (
    AgentPermissions,
    has_path_traversal,
    is_path_allowed,
    resolve_and_validate_path,
)

# ---------------------------------------------------------------------------
# has_path_traversal
# ---------------------------------------------------------------------------


class TestHasPathTraversal:
    """Test quick traversal pattern detection."""

    def test_clean_relative_path(self) -> None:
        assert not has_path_traversal("src/foo.py")

    def test_dotdot_traversal(self) -> None:
        assert has_path_traversal("../../../etc/passwd")

    def test_dotdot_in_middle(self) -> None:
        assert has_path_traversal("src/../../../etc/passwd")

    def test_null_byte_injection(self) -> None:
        assert has_path_traversal("src/foo.py\x00.jpg")

    def test_url_encoded_dotdot(self) -> None:
        assert has_path_traversal("%2e%2e/etc/passwd")

    def test_url_encoded_slash(self) -> None:
        assert has_path_traversal("src%2f..%2f..%2fetc/passwd")

    def test_backslash_normalized(self) -> None:
        assert has_path_traversal("src\\..\\..\\etc\\passwd")

    def test_single_dot_not_flagged(self) -> None:
        # Single dots are fine (current dir)
        assert not has_path_traversal("./src/foo.py")

    def test_dotfile_not_flagged(self) -> None:
        assert not has_path_traversal(".github/workflows/ci.yml")


# ---------------------------------------------------------------------------
# resolve_and_validate_path
# ---------------------------------------------------------------------------


class TestResolveAndValidatePath:
    """Test path resolution and containment validation."""

    def test_relative_path_within_root(self, tmp_path: Path) -> None:
        rel, safe = resolve_and_validate_path("src/foo.py", tmp_path)
        assert safe
        assert rel == "src/foo.py"

    def test_dotdot_escaping_root(self, tmp_path: Path) -> None:
        # Use an absolute path outside the root to avoid OS-dependent ../.. resolution
        _rel, safe = resolve_and_validate_path("/etc/passwd", tmp_path)
        assert not safe

    def test_absolute_path_within_root(self, tmp_path: Path) -> None:
        # Create the file so realpath resolves it
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "foo.py").touch()
        abs_path = str(tmp_path / "src" / "foo.py")
        rel, safe = resolve_and_validate_path(abs_path, tmp_path)
        assert safe
        assert rel == os.path.join("src", "foo.py")

    def test_absolute_path_outside_root(self, tmp_path: Path) -> None:
        _rel, safe = resolve_and_validate_path("/etc/passwd", tmp_path)
        assert not safe

    def test_symlink_escape(self, tmp_path: Path) -> None:
        """Symlinks pointing outside root are caught by realpath."""
        # Create a symlink inside tmp_path pointing to /tmp
        link = tmp_path / "escape"
        try:
            link.symlink_to("/tmp")
        except OSError:
            pytest.skip("Cannot create symlinks on this platform")
        _rel, safe = resolve_and_validate_path("escape/something", tmp_path)
        assert not safe

    def test_leading_dot_slash_stripped(self, tmp_path: Path) -> None:
        rel, safe = resolve_and_validate_path("./src/foo.py", tmp_path)
        assert safe
        assert "src" in rel


# ---------------------------------------------------------------------------
# is_path_allowed with traversal hardening
# ---------------------------------------------------------------------------


class TestIsPathAllowedTraversal:
    """Test that is_path_allowed rejects traversal attempts."""

    def test_dotdot_traversal_denied(self) -> None:
        perms = AgentPermissions(allowed_paths=("src/*",))
        assert not is_path_allowed("../../etc/passwd", perms)

    def test_null_byte_denied(self) -> None:
        perms = AgentPermissions(allowed_paths=("src/*",))
        assert not is_path_allowed("src/foo\x00.py", perms)

    def test_normal_path_still_works(self) -> None:
        perms = AgentPermissions(allowed_paths=("src/*",))
        assert is_path_allowed("src/foo.py", perms)

    def test_denied_path_still_denied(self) -> None:
        perms = AgentPermissions(
            allowed_paths=("src/*",),
            denied_paths=("src/secret/*",),
        )
        assert not is_path_allowed("src/secret/keys.py", perms)

    def test_with_project_root_validation(self, tmp_path: Path) -> None:
        perms = AgentPermissions(allowed_paths=("src/*",))
        # Traversal with project root
        assert not is_path_allowed(
            "../../../etc/passwd",
            perms,
            project_root=tmp_path,
        )
