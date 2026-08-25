"""Tests for ``test_passes`` signals whose test path mirrors the ``src`` layout.

A completion signal names its test by path, and that path is written when the
task is planned, not read off the tree. The two drift: a plan that mirrors
``src/bernstein/core/security/`` asks for
``tests/unit/core/security/test_policy.py`` while the suite keeps that file at
``tests/unit/test_policy.py``. pytest then exits during collection without
running a single test, and the janitor recorded that exit as the agent's work
being wrong -- so the agent was rejected over a path it never chose.

``_resolve_test_path_command`` rewrites such a command onto the file that
exists, but only when the rename is unambiguous. The tests below pin both
halves: the rewrite happens for a unique basename, and it deliberately does
*not* happen when the file is absent everywhere (the agent really was asked to
write it) or when several files share the name.
"""

from __future__ import annotations

import sys
from pathlib import Path

from bernstein.core.models import CompletionSignal

from bernstein.core.quality.janitor import (
    _resolve_test_path_command,
    evaluate_signal,
)


def _touch(path: Path, body: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def test_unique_basename_is_rewritten_onto_the_file_that_exists(tmp_path: Path) -> None:
    _touch(tmp_path / "tests/unit/test_policy.py")
    command = "uv run pytest tests/unit/core/security/test_policy.py -x -q"

    assert _resolve_test_path_command(command, tmp_path) == "uv run pytest tests/unit/test_policy.py -x -q"


def test_every_missing_path_in_a_multi_file_command_is_rewritten(tmp_path: Path) -> None:
    _touch(tmp_path / "tests/unit/routing/test_router_core.py")
    _touch(tmp_path / "tests/unit/test_fast_path.py")
    command = "uv run pytest tests/unit/core/routing/test_router_core.py tests/unit/core/quality/test_fast_path.py -q"

    assert _resolve_test_path_command(command, tmp_path) == (
        "uv run pytest tests/unit/routing/test_router_core.py tests/unit/test_fast_path.py -q"
    )


def test_node_id_survives_the_rewrite(tmp_path: Path) -> None:
    _touch(tmp_path / "tests/unit/test_policy.py")
    command = "pytest tests/unit/core/security/test_policy.py::test_denies -q"

    assert _resolve_test_path_command(command, tmp_path) == "pytest tests/unit/test_policy.py::test_denies -q"


def test_absent_everywhere_keeps_the_original_path(tmp_path: Path) -> None:
    """The agent was asked to write this test and did not. That must still fail."""
    (tmp_path / "tests/unit").mkdir(parents=True)
    command = "uv run pytest tests/unit/evolution/test_applicator.py -x -q"

    assert _resolve_test_path_command(command, tmp_path) == command


def test_ambiguous_basename_keeps_the_original_path(tmp_path: Path) -> None:
    _touch(tmp_path / "tests/unit/test_policy.py")
    _touch(tmp_path / "tests/integration/test_policy.py")
    command = "pytest tests/unit/core/security/test_policy.py -q"

    assert _resolve_test_path_command(command, tmp_path) == command


def test_existing_path_is_left_untouched(tmp_path: Path) -> None:
    _touch(tmp_path / "tests/unit/test_policy.py")
    _touch(tmp_path / "tests/integration/test_policy.py")
    command = "pytest tests/unit/test_policy.py -q"

    assert _resolve_test_path_command(command, tmp_path) == command


def test_search_never_leaves_the_declared_root(tmp_path: Path) -> None:
    """A match inside an installed package is not a match for the suite."""
    _touch(tmp_path / ".venv/lib/site-packages/pkg/test_policy.py")
    (tmp_path / "tests").mkdir()
    command = "pytest tests/unit/core/security/test_policy.py -q"

    assert _resolve_test_path_command(command, tmp_path) == command


def test_signal_passes_once_the_moved_test_is_found(tmp_path: Path) -> None:
    _touch(
        tmp_path / "tests/unit/test_policy.py",
        "def test_denies() -> None:\n    assert True\n",
    )
    signal = CompletionSignal(
        type="test_passes",
        value=f"{sys.executable} -m pytest tests/unit/core/security/test_policy.py -q -p no:cacheprovider",
    )

    passed, _ = evaluate_signal(signal, tmp_path)

    assert passed


def test_signal_still_fails_when_the_test_was_never_written(tmp_path: Path) -> None:
    (tmp_path / "tests/unit").mkdir(parents=True)
    signal = CompletionSignal(
        type="test_passes",
        value=f"{sys.executable} -m pytest tests/unit/evolution/test_applicator.py -q -p no:cacheprovider",
    )

    passed, _ = evaluate_signal(signal, tmp_path)

    assert not passed
