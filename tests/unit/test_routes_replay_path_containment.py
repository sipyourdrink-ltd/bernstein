"""Containment tests for the four route and replay path checks (issue #3822).

One escape test per converted site, each paired with a positive control.

The two ``review_board`` modules implement *different* surfaces -- a run
directory in ``routes``, a per-run diff file in ``replay`` -- and they had
drifted in what they proved:

* ``routes._contained_run_dir`` short-circuited on ``resolved != runs_root``,
  so an id resolving *to* the runs root itself was accepted.
* ``replay._contained_diff_path`` contained the ``task_id`` only. Its
  docstring promised defence in depth against a crafted ``run_id`` as well,
  but the ``run_id`` was interpolated into the base it then checked against,
  so it could not refuse one.

A refusal on a request path must name the rejected identifier and never the
absolute layout; the tests assert that property directly.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from bernstein.core.replay.review_board import REVIEW_DIFF_SUBDIR, _contained_diff_path
from bernstein.core.routes.approvals import _safe_child as approvals_safe_child
from bernstein.core.routes.review_board import _contained_run_dir
from bernstein.core.server.hooks_receiver import InvalidSessionIdError
from bernstein.core.server.hooks_receiver import _safe_child as hooks_safe_child

#: Ids that resolve outside, or exactly onto, the base they are joined under.
#: Each is accepted by the callers' own slug patterns.
ESCAPING_IDS = ["..", "."]


# ---------------------------------------------------------------------------
# routes/review_board.py - _contained_run_dir
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("run_id", ESCAPING_IDS)
def test_contained_run_dir_refuses_non_descendant_run_id(tmp_path: Path, run_id: str) -> None:
    """``.`` resolved to the runs root itself and was let through."""
    sdd_dir = tmp_path / ".sdd"
    (sdd_dir / "runs").mkdir(parents=True)

    with pytest.raises(HTTPException) as exc_info:
        _contained_run_dir(sdd_dir, run_id)
    assert exc_info.value.status_code == 400


def test_contained_run_dir_accepts_an_ordinary_run_id(tmp_path: Path) -> None:
    """Positive control: a real run directory still resolves."""
    sdd_dir = tmp_path / ".sdd"
    run_dir = sdd_dir / "runs" / "run-123"
    run_dir.mkdir(parents=True)

    assert _contained_run_dir(sdd_dir, "run-123") == run_dir.resolve()


def test_contained_run_dir_refusal_does_not_leak_the_base(tmp_path: Path) -> None:
    """The 400 detail must not carry the absolute layout."""
    sdd_dir = tmp_path / ".sdd"
    (sdd_dir / "runs").mkdir(parents=True)

    with pytest.raises(HTTPException) as exc_info:
        _contained_run_dir(sdd_dir, "..")
    assert str(tmp_path) not in str(exc_info.value.detail)


# ---------------------------------------------------------------------------
# replay/review_board.py - _contained_diff_path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("run_id", ESCAPING_IDS)
def test_contained_diff_path_refuses_escaping_run_id(tmp_path: Path, run_id: str) -> None:
    """A crafted run_id must not move the diffs root out of the runs root.

    Before this change the run_id was interpolated into the base, so
    ``run_id=".."`` yielded ``.sdd/review/diffs/<task>.diff`` -- outside
    ``.sdd/runs/`` entirely -- and the containment check still passed, because
    it compared the candidate against that relocated base.
    """
    sdd_dir = tmp_path / ".sdd"
    (sdd_dir / "runs").mkdir(parents=True)

    assert _contained_diff_path(sdd_dir, run_id, "task-1") is None


def test_contained_diff_path_refuses_escaping_task_id(tmp_path: Path) -> None:
    """The pre-existing task_id refusal survives the conversion."""
    sdd_dir = tmp_path / ".sdd"
    (sdd_dir / "runs").mkdir(parents=True)

    assert _contained_diff_path(sdd_dir, "run-123", "../../escape") is None


def test_contained_diff_path_accepts_an_ordinary_pair(tmp_path: Path) -> None:
    """Positive control: an ordinary run/task pair still resolves in place."""
    sdd_dir = tmp_path / ".sdd"
    diffs_root = sdd_dir / "runs" / "run-123" / REVIEW_DIFF_SUBDIR
    diffs_root.mkdir(parents=True)

    path = _contained_diff_path(sdd_dir, "run-123", "task-1")

    assert path is not None
    assert path == (diffs_root / "task-1.diff").resolve()
    assert path.parent == diffs_root.resolve()


def test_contained_diff_path_refuses_symlinked_diffs_dir(tmp_path: Path) -> None:
    """A symlinked diffs directory cannot relocate the write out of the run."""
    sdd_dir = tmp_path / ".sdd"
    review = sdd_dir / "runs" / "run-123" / "review"
    review.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (review / "diffs").symlink_to(outside, target_is_directory=True)

    assert _contained_diff_path(sdd_dir, "run-123", "task-1") is None


# ---------------------------------------------------------------------------
# routes/approvals.py - _safe_child
# ---------------------------------------------------------------------------


def test_approvals_safe_child_refuses_symlinked_child(tmp_path: Path) -> None:
    """A symlinked entry in the approvals directory cannot capture the path."""
    base = tmp_path / "pending"
    base.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim.json").write_text("{}", encoding="utf-8")
    (base / "task-1.json").symlink_to(outside / "victim.json")

    with pytest.raises(HTTPException) as exc_info:
        approvals_safe_child(base, "task-1.json")
    assert exc_info.value.status_code == 400
    assert str(tmp_path) not in str(exc_info.value.detail)


def test_approvals_safe_child_accepts_an_ordinary_filename(tmp_path: Path) -> None:
    """Positive control: an ordinary filename still resolves under the base."""
    base = tmp_path / "pending"
    base.mkdir()

    assert approvals_safe_child(base, "task-1.json") == (base / "task-1.json").resolve()


# ---------------------------------------------------------------------------
# server/hooks_receiver.py - _safe_child
# ---------------------------------------------------------------------------


def test_hooks_safe_child_refuses_symlinked_sidecar(tmp_path: Path) -> None:
    """A symlinked sidecar cannot redirect hook writes out of the hooks dir."""
    base = tmp_path / "hooks"
    base.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim.jsonl").write_text("", encoding="utf-8")
    (base / "sess-1.jsonl").symlink_to(outside / "victim.jsonl")

    with pytest.raises(InvalidSessionIdError) as exc_info:
        hooks_safe_child(base, "sess-1", suffix=".jsonl")
    assert str(tmp_path) not in str(exc_info.value)


def test_hooks_safe_child_accepts_an_ordinary_session_id(tmp_path: Path) -> None:
    """Positive control: an ordinary session id still resolves."""
    base = tmp_path / "hooks"
    base.mkdir()

    assert hooks_safe_child(base, "sess-1", suffix=".jsonl") == (base / "sess-1.jsonl").resolve()
