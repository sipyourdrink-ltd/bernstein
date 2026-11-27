"""``resolve_session_journal_path`` refuses every symlinked journal shape.

``contained_path`` follows links, so a ``journal.jsonl`` that points at a
file inside its own run directory still resolves inside the run directory.
Only the canonical-location check in ``resolve_session_journal_path``
refuses those shapes; every refusal case here fails if that check goes.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from bernstein.core.protocols.acp.session import resolve_session_journal_path

_RUN_ID = "run-1"


def _layout(tmp_path: Path) -> tuple[Path, Path]:
    sdd = tmp_path.resolve() / ".sdd"
    run_dir = sdd / "runs" / _RUN_ID
    run_dir.mkdir(parents=True)
    return sdd, run_dir


def _link_or_skip(link: Path, target: str | Path) -> None:
    """Create *link* -> *target*, or skip where the filesystem cannot.

    Unprivileged Windows runners cannot create symlinks, and a
    case-insensitive filesystem cannot hold a case-variant sibling.
    """
    try:
        link.symlink_to(target)
    except OSError:  # pragma: no cover - platform dependent
        pytest.skip("cannot create this link on this filesystem")


def _write(path: Path) -> None:
    path.write_text('{"event": "x"}\n', encoding="utf-8")


def _sibling_relative(run_dir: Path) -> None:
    _write(run_dir / "other.jsonl")
    _link_or_skip(run_dir / "journal.jsonl", "other.jsonl")


def _sibling_absolute(run_dir: Path) -> None:
    _write(run_dir / "other.jsonl")
    _link_or_skip(run_dir / "journal.jsonl", run_dir / "other.jsonl")


def _nested_journal(run_dir: Path) -> None:
    (run_dir / "sub").mkdir()
    _write(run_dir / "sub" / "journal.jsonl")
    _link_or_skip(run_dir / "journal.jsonl", Path("sub") / "journal.jsonl")


def _case_variant_sibling(run_dir: Path) -> None:
    _write(run_dir / "JOURNAL.jsonl")
    _link_or_skip(run_dir / "journal.jsonl", "JOURNAL.jsonl")


def _dangling(run_dir: Path) -> None:
    _link_or_skip(run_dir / "journal.jsonl", "missing.jsonl")


def _self_loop(run_dir: Path) -> None:
    _link_or_skip(run_dir / "journal.jsonl", "journal.jsonl")


def _two_link_loop(run_dir: Path) -> None:
    _link_or_skip(run_dir / "b.jsonl", "journal.jsonl")
    _link_or_skip(run_dir / "journal.jsonl", "b.jsonl")


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(_sibling_relative, id="sibling_relative"),
        pytest.param(_sibling_absolute, id="sibling_absolute"),
        pytest.param(_nested_journal, id="nested_journal"),
        pytest.param(_case_variant_sibling, id="case_variant_sibling"),
        pytest.param(_dangling, id="dangling"),
        pytest.param(_self_loop, id="self_loop"),
        pytest.param(_two_link_loop, id="two_link_loop"),
    ],
)
def test_symlinked_journal_inside_run_dir_is_refused(tmp_path: Path, build: Callable[[Path], None]) -> None:
    sdd, run_dir = _layout(tmp_path)
    build(run_dir)

    assert resolve_session_journal_path(sdd, _RUN_ID) == (None, False)


def test_regular_journal_is_accepted(tmp_path: Path) -> None:
    sdd, run_dir = _layout(tmp_path)
    _write(run_dir / "journal.jsonl")

    jpath, ok = resolve_session_journal_path(sdd, _RUN_ID)

    assert ok is True
    assert jpath == run_dir / "journal.jsonl"


def test_missing_journal_is_not_an_error(tmp_path: Path) -> None:
    sdd, _run_dir = _layout(tmp_path)

    assert resolve_session_journal_path(sdd, _RUN_ID) == (None, True)
