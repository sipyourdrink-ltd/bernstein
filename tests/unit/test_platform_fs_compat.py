"""Filesystem platform layer for worktree handling (issue #2367).

Covers the Windows filesystem semantics that worktree management relies on:

* ``to_extended_length_path`` - extended-length (``\\\\?\\``) prefixing for
  paths that exceed the legacy Windows path limit.  Pass-through on POSIX.
* ``robust_rmtree`` - tree removal that clears the Windows read-only
  attribute and retries transient sharing violations.  Single attempt on
  POSIX, byte-identical to ``shutil.rmtree``.
* ``is_filesystem_link`` - link detection that treats NTFS junctions the
  same as symlinks, so isolation checks cannot be bypassed by a junction.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from bernstein.core.platform_compat import (
    IS_WINDOWS,
    is_filesystem_link,
    robust_rmtree,
    to_extended_length_path,
)

_PC = "bernstein.core.config.platform_compat"


# ---------------------------------------------------------------------------
# to_extended_length_path
# ---------------------------------------------------------------------------


class TestToExtendedLengthPath:
    def test_posix_passthrough(self) -> None:
        if IS_WINDOWS:
            pytest.skip("POSIX passthrough asserted under mock below")
        assert to_extended_length_path("/usr/local/bin") == "/usr/local/bin"

    @patch(f"{_PC}.IS_WINDOWS", False)
    def test_passthrough_preserves_relative_paths(self) -> None:
        assert to_extended_length_path("src/pkg") == "src/pkg"

    @patch(f"{_PC}.IS_WINDOWS", True)
    def test_short_windows_path_unprefixed(self) -> None:
        result = to_extended_length_path("C:\\repo\\worktree")
        assert not result.startswith("\\\\?\\")

    @patch(f"{_PC}.IS_WINDOWS", True)
    def test_long_windows_path_gets_prefix(self) -> None:
        long_path = "C:\\" + "\\".join(["deep-worktree-segment"] * 16)
        assert len(long_path) >= 248
        result = to_extended_length_path(long_path)
        assert result.startswith("\\\\?\\C:")

    @patch(f"{_PC}.IS_WINDOWS", True)
    def test_already_prefixed_unchanged(self) -> None:
        prefixed = "\\\\?\\C:\\" + "x" * 300
        assert to_extended_length_path(prefixed) == prefixed

    @patch(f"{_PC}.IS_WINDOWS", True)
    def test_long_unc_path_gets_unc_prefix(self) -> None:
        unc = "\\\\server\\share\\" + "\\".join(["seg"] * 80)
        assert len(unc) >= 248
        result = to_extended_length_path(unc)
        assert result.startswith("\\\\?\\UNC\\server\\share")

    @patch(f"{_PC}.IS_WINDOWS", True)
    def test_prefixing_is_deterministic(self) -> None:
        long_path = "C:\\" + "y" * 300
        assert to_extended_length_path(long_path) == to_extended_length_path(long_path)


# ---------------------------------------------------------------------------
# robust_rmtree
# ---------------------------------------------------------------------------


class TestRobustRmtree:
    def test_removes_populated_tree(self, tmp_path: Path) -> None:
        tree = tmp_path / "victim"
        (tree / "nested").mkdir(parents=True)
        (tree / "nested" / "file.txt").write_text("payload", encoding="utf-8")
        assert robust_rmtree(tree) is True
        assert not tree.exists()

    def test_missing_path_is_success(self, tmp_path: Path) -> None:
        assert robust_rmtree(tmp_path / "never-existed") is True

    def test_posix_failure_returns_false(self, tmp_path: Path) -> None:
        tree = tmp_path / "victim"
        tree.mkdir()
        with (
            patch(f"{_PC}.IS_WINDOWS", False),
            patch(f"{_PC}.shutil.rmtree", side_effect=OSError("boom")),
        ):
            assert robust_rmtree(tree) is False

    def test_windows_retries_transient_lock(self, tmp_path: Path) -> None:
        """A sharing-violation style failure is retried, then succeeds."""
        tree = tmp_path / "victim"
        tree.mkdir()
        calls: list[int] = []

        def _flaky(path: object, **_kwargs: object) -> None:
            calls.append(1)
            if len(calls) == 1:
                raise OSError("access denied (simulated file lock)")

        with (
            patch(f"{_PC}.IS_WINDOWS", True),
            patch(f"{_PC}.shutil.rmtree", side_effect=_flaky),
            patch(f"{_PC}.time.sleep"),
        ):
            assert robust_rmtree(tree) is True
        assert len(calls) == 2

    def test_windows_gives_up_after_max_attempts(self, tmp_path: Path) -> None:
        tree = tmp_path / "victim"
        tree.mkdir()
        with (
            patch(f"{_PC}.IS_WINDOWS", True),
            patch(f"{_PC}.shutil.rmtree", side_effect=OSError("still locked")),
            patch(f"{_PC}.time.sleep") as mock_sleep,
        ):
            assert robust_rmtree(tree, max_attempts=3) is False
        assert mock_sleep.call_count == 2  # no sleep after the final attempt

    def test_readonly_file_removed(self, tmp_path: Path) -> None:
        """Read-only entries must not survive removal (Windows attribute case)."""
        tree = tmp_path / "victim"
        tree.mkdir()
        target = tree / "readonly.txt"
        target.write_text("locked", encoding="utf-8")
        target.chmod(0o444)
        assert robust_rmtree(tree) is True
        assert not tree.exists()


# ---------------------------------------------------------------------------
# is_filesystem_link
# ---------------------------------------------------------------------------


class _JunctionStub:
    """Duck-typed Path stand-in modelling an NTFS junction."""

    def is_symlink(self) -> bool:
        return False

    def is_junction(self) -> bool:
        return True


class _BrokenStub:
    """Duck-typed Path stand-in whose probes raise OSError."""

    def is_symlink(self) -> bool:
        raise OSError("unreadable")

    def is_junction(self) -> bool:  # pragma: no cover - never reached
        raise OSError("unreadable")


class TestIsFilesystemLink:
    def test_regular_dir_is_not_link(self, tmp_path: Path) -> None:
        target = tmp_path / "plain"
        target.mkdir()
        assert is_filesystem_link(target) is False

    def test_symlink_detected(self, tmp_path: Path) -> None:
        if IS_WINDOWS:
            pytest.skip("symlink creation needs Developer Mode on Windows")
        source = tmp_path / "src"
        source.mkdir()
        link = tmp_path / "lnk"
        link.symlink_to(source)
        assert is_filesystem_link(link) is True

    def test_junction_detected(self) -> None:
        assert is_filesystem_link(_JunctionStub()) is True  # type: ignore[arg-type]

    def test_probe_error_is_not_link(self) -> None:
        assert is_filesystem_link(_BrokenStub()) is False  # type: ignore[arg-type]

    def test_missing_path_is_not_link(self, tmp_path: Path) -> None:
        assert is_filesystem_link(tmp_path / "absent") is False
