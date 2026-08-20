"""Containment for the approvals path derivation (#4034).

``approval_path_in`` fails closed through two independent gates: the
identifier allowlist, then a resolved-path containment check. These tests pin
the second one -- specifically the invariant its docstring names, that a
symlinked approvals *directory* is followed while a decision *file* symlinked
out of that directory is refused. The allowlist alone cannot tell those apart.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from bernstein.core.orchestration.approval_gate import (
    UnsafeApprovalIdError,
    approval_path_in,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_approval_path_in_returns_a_contained_child(tmp_path: Path) -> None:
    approvals = tmp_path / "approvals"
    approvals.mkdir()

    path = approval_path_in(approvals, "task-1", ".approved")

    assert path == approvals.resolve() / "task-1.approved"


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_approval_path_in_follows_a_symlinked_approvals_dir(tmp_path: Path) -> None:
    """The base is resolved, so a relocated approvals directory still works.

    This half of the invariant is what stops the containment check from
    refusing a perfectly ordinary deployment that symlinks its runtime
    directory elsewhere.
    """
    real = tmp_path / "real-approvals"
    real.mkdir()
    linked = tmp_path / "approvals"
    linked.symlink_to(real)

    path = approval_path_in(linked, "task-1", ".approved")

    assert path == real.resolve() / "task-1.approved"


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_approval_path_in_refuses_symlinked_approvals_dir_escape(tmp_path: Path) -> None:
    """A decision file symlinked out of the approvals directory is refused."""
    approvals = tmp_path / "approvals"
    approvals.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (approvals / "task-1.approved").symlink_to(outside / "task-1.approved")

    with pytest.raises(UnsafeApprovalIdError, match="refusing approvals path outside"):
        approval_path_in(approvals, "task-1", ".approved")


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_approval_path_in_refuses_a_decision_file_symlinked_deeper(tmp_path: Path) -> None:
    """Contained is not sufficient: decision files are direct children."""
    approvals = tmp_path / "approvals"
    (approvals / "sub").mkdir(parents=True)
    (approvals / "sub" / "x").write_text("planted\n", encoding="utf-8")
    (approvals / "task-1.approved").symlink_to(approvals / "sub" / "x")

    with pytest.raises(UnsafeApprovalIdError):
        approval_path_in(approvals, "task-1", ".approved")


@pytest.mark.parametrize("approval_id", ["../escape", "..", "nested/child", "", "-leading"])
def test_approval_path_in_refuses_unsafe_ids(tmp_path: Path, approval_id: str) -> None:
    """The allowlist gate still refuses, and still with the same error type."""
    approvals = tmp_path / "approvals"
    approvals.mkdir()

    with pytest.raises(UnsafeApprovalIdError):
        approval_path_in(approvals, approval_id, ".approved")
