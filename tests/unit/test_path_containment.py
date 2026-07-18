"""Path-containment regression tests for identifier-derived filesystem paths.

Run ids, task ids, mission ids, and ledger ids arrive from the dashboard
API, the MCP surface, and the CLI, and each of them names a directory or a
file under ``.sdd``. These tests pin the barrier at every site that turns
such an identifier into a path: a traversal-shaped, absolute, or
symlink-escaping identifier is refused with a typed error and leaves
nothing on disk outside the intended base, while an ordinary identifier
still round-trips unchanged.
"""

# The task journal path helper is module-private but is the exact barrier
# under test, so it is exercised directly rather than through a caller.
# pyright: reportPrivateUsage=false

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.admission.ledger import admission_ledger_dir
from bernstein.core.evidence.run_artifacts import _artifact_journal_path
from bernstein.core.persistence.work_ledger import (
    KIND_RUN_OPEN,
    LedgerReader,
    WorkLedger,
    default_ledger_root,
    run_ledger_dir,
)
from bernstein.core.replay.journal import EventJournal, JournalPathError
from bernstein.core.security.path_containment import (
    PathContainmentError,
    contained_path,
    validate_path_segment,
)

#: Identifiers that must never be accepted as a single path segment.
HOSTILE_IDS = [
    "..",
    ".",
    "../escape",
    "../../etc",
    "a/../../b",
    "sub/dir",
    "/etc/passwd",
    "/absolute",
    "",
    "with space",
    "semi;colon",
    "null\x00byte",
    "back\\slash",
]


def _symlink_or_skip(link: Path, target: Path, *, directory: bool = True) -> None:
    """Create *link* pointing at *target*, or skip where that is not allowed.

    Unprivileged Windows runners cannot create symlinks, and the symlink
    leg of these tests is the only part that needs one; the allowlist leg
    still runs everywhere.
    """
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError:  # pragma: no cover - platform dependent
        pytest.skip("cannot create symlinks on this platform")


# ---------------------------------------------------------------------------
# The barrier itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_id", HOSTILE_IDS)
def test_validate_path_segment_refuses_unsafe_ids(bad_id: str) -> None:
    """Every traversal or separator shape is refused with the typed error."""
    with pytest.raises(PathContainmentError):
        validate_path_segment(bad_id, label="run id")


@pytest.mark.parametrize("good_id", ["run-1", "run_1", "task-T.1", "a", "fleet", "A" * 255])
def test_validate_path_segment_accepts_ordinary_ids(good_id: str) -> None:
    """A plain identifier passes through unchanged."""
    assert validate_path_segment(good_id) == good_id


def test_validate_path_segment_refuses_overlong_id() -> None:
    """A name past NAME_MAX is refused as a typed error, not an OSError."""
    with pytest.raises(PathContainmentError):
        validate_path_segment("A" * 256)


def test_contained_path_joins_under_base(tmp_path: Path) -> None:
    """An ordinary id resolves to the expected child of the base."""
    resolved = contained_path(tmp_path, "run-1", "journal.jsonl")
    assert resolved == (tmp_path / "run-1" / "journal.jsonl").resolve()


@pytest.mark.parametrize("bad_id", HOSTILE_IDS)
def test_contained_path_refuses_unsafe_ids(tmp_path: Path, bad_id: str) -> None:
    """A hostile id never produces a path outside the base."""
    with pytest.raises(PathContainmentError):
        contained_path(tmp_path, bad_id)


def test_contained_path_refuses_symlink_escape(tmp_path: Path) -> None:
    """A well-named child that symlinks out of the base is refused.

    This is the case the identifier allowlist cannot catch on its own: the
    segment is a perfectly ordinary name, but following it leaves the tree.
    """
    base = tmp_path / "base"
    base.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    _symlink_or_skip(base / "escape", outside)

    with pytest.raises(PathContainmentError):
        contained_path(base, "escape")


def test_contained_path_requires_a_segment(tmp_path: Path) -> None:
    """The result must be a strict descendant, so a bare base is refused."""
    with pytest.raises(PathContainmentError):
        contained_path(tmp_path)


def test_contained_path_refuses_sibling_prefix(tmp_path: Path) -> None:
    """A sibling sharing the base's name prefix must not pass containment."""
    base = tmp_path / "base"
    base.mkdir()
    sibling = tmp_path / "base-evil"
    sibling.mkdir()
    _symlink_or_skip(base / "link", sibling)

    with pytest.raises(PathContainmentError):
        contained_path(base, "link")


def test_contained_path_allows_symlink_inside_base(tmp_path: Path) -> None:
    """A symlink that stays inside the base is fine - containment, not a ban."""
    base = tmp_path / "base"
    (base / "real").mkdir(parents=True)
    _symlink_or_skip(base / "link", base / "real")

    assert contained_path(base, "link") == (base / "real").resolve()


# ---------------------------------------------------------------------------
# EventJournal (replay journal path)
# ---------------------------------------------------------------------------


def test_event_journal_round_trips_ordinary_run_id(tmp_path: Path) -> None:
    """The on-disk layout for a legitimate run id is unchanged."""
    journal = EventJournal(run_id="run-1", sdd_dir=tmp_path)
    journal.record("task_claimed", task_id="T-1")

    assert journal.path == (tmp_path / "runs" / "run-1" / "journal.jsonl").resolve()
    assert journal.path.is_file()
    assert journal.event_count() == 1
    assert journal.verify().ok


@pytest.mark.parametrize("bad_id", ["..", ".", "../escape", "sub/dir", "/etc/passwd", ""])
def test_event_journal_refuses_traversal_run_id(tmp_path: Path, bad_id: str) -> None:
    """A traversal run id is refused and writes nothing outside the runs root."""
    sdd_dir = tmp_path / ".sdd"
    sdd_dir.mkdir()

    with pytest.raises(JournalPathError):
        EventJournal(run_id=bad_id, sdd_dir=sdd_dir)

    assert list(tmp_path.iterdir()) == [sdd_dir]
    assert not any(sdd_dir.rglob("journal.jsonl"))


def test_event_journal_path_error_is_a_value_error(tmp_path: Path) -> None:
    """Callers that guard on ValueError keep catching a bad run id."""
    with pytest.raises(ValueError, match="unsafe run_id"):
        EventJournal(run_id="../escape", sdd_dir=tmp_path)


def test_event_journal_refuses_symlinked_run_dir(tmp_path: Path) -> None:
    """A run directory symlinked out of the runs root is refused."""
    sdd_dir = tmp_path / ".sdd"
    runs_root = sdd_dir / "runs"
    runs_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    _symlink_or_skip(runs_root / "run-evil", outside)

    with pytest.raises(JournalPathError):
        EventJournal(run_id="run-evil", sdd_dir=sdd_dir)

    assert list(outside.iterdir()) == []


# ---------------------------------------------------------------------------
# Task artifact journal path
# ---------------------------------------------------------------------------


def test_artifact_journal_path_round_trips(tmp_path: Path) -> None:
    """A normal task id maps to the same journal path as before."""
    assert _artifact_journal_path(tmp_path, "T-1") == (tmp_path / "runs" / "task-T-1" / "journal.jsonl").resolve()


@pytest.mark.parametrize("bad_id", ["../../etc/passwd", "/etc/passwd", "a/../../b"])
def test_artifact_journal_path_neutralises_traversal(tmp_path: Path, bad_id: str) -> None:
    """A traversal task id is slugified and stays under the runs root."""
    resolved = _artifact_journal_path(tmp_path, bad_id)
    assert resolved.is_relative_to((tmp_path / "runs").resolve())


def test_artifact_journal_path_refuses_symlinked_run_dir(tmp_path: Path) -> None:
    """A symlinked task run directory is refused rather than followed."""
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    _symlink_or_skip(runs_root / "task-T-1", outside)

    with pytest.raises(PathContainmentError):
        _artifact_journal_path(tmp_path, "T-1")


# ---------------------------------------------------------------------------
# Work ledger directories and buckets
# ---------------------------------------------------------------------------


def test_run_ledger_dir_round_trips(tmp_path: Path) -> None:
    """A legitimate run id keeps the documented ledger layout."""
    assert run_ledger_dir(tmp_path, "run-a") == (tmp_path / "runtime" / "ledger" / "run-a").resolve()


@pytest.mark.parametrize("bad_id", HOSTILE_IDS)
def test_run_ledger_dir_refuses_unsafe_run_id(tmp_path: Path, bad_id: str) -> None:
    """A hostile run (or mission) id never escapes the ledger root."""
    with pytest.raises(PathContainmentError):
        run_ledger_dir(tmp_path, bad_id)


def test_run_ledger_dir_refuses_symlink_escape(tmp_path: Path) -> None:
    """A ledger directory symlinked out of the root is refused."""
    root = default_ledger_root(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "run-evil").symlink_to(outside, target_is_directory=True)

    with pytest.raises(PathContainmentError):
        run_ledger_dir(tmp_path, "run-evil")

    assert list(outside.iterdir()) == []


def test_ledger_round_trips_through_reader(tmp_path: Path) -> None:
    """Writer and reader agree on the bucket for an ordinary ledger dir."""
    ledger_dir = run_ledger_dir(tmp_path, "run-a")
    ledger = WorkLedger.open(ledger_dir)
    ledger.append(kind=KIND_RUN_OPEN, payload={"goal": "ship it"})
    ledger.close()

    reader = LedgerReader(ledger_dir)
    assert reader.exists()
    assert reader.bucket_path == ledger_dir / "000000.jsonl"
    assert [entry.kind for entry in reader.entries()] == [KIND_RUN_OPEN]
    assert reader.verify().ok


def test_ledger_reader_refuses_symlinked_bucket(tmp_path: Path) -> None:
    """A bucket file symlinked out of the ledger directory is refused.

    Without the barrier a reader would follow the link and disclose the
    target file's bytes as if they were ledger rows.
    """
    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir()
    secret = tmp_path / "secret.jsonl"
    secret.write_text("{}\n", encoding="utf-8")
    _symlink_or_skip(ledger_dir / "000000.jsonl", secret, directory=False)

    with pytest.raises(PathContainmentError):
        LedgerReader(ledger_dir)

    with pytest.raises(PathContainmentError):
        WorkLedger.open(ledger_dir)

    assert secret.read_text(encoding="utf-8") == "{}\n"


# ---------------------------------------------------------------------------
# Admission ledger directory
# ---------------------------------------------------------------------------


def test_admission_ledger_dir_round_trips(tmp_path: Path) -> None:
    """The default admission ledger keeps its documented location."""
    assert admission_ledger_dir(tmp_path) == (tmp_path / "runtime" / "admission" / "fleet").resolve()


@pytest.mark.parametrize("bad_id", HOSTILE_IDS)
def test_admission_ledger_dir_refuses_unsafe_id(tmp_path: Path, bad_id: str) -> None:
    """A hostile ledger id never escapes the admission root."""
    with pytest.raises(PathContainmentError):
        admission_ledger_dir(tmp_path, bad_id)


def test_no_stray_writes_outside_base(tmp_path: Path) -> None:
    """A refused identifier creates nothing outside the intended base.

    ``default_ledger_root`` legitimately creates its own root on first use;
    what must never appear is a directory named after the hostile id, or
    anything at all beside the ``.sdd`` tree.
    """
    sdd_dir = tmp_path / ".sdd"
    sdd_dir.mkdir()

    for bad_id in HOSTILE_IDS:
        with pytest.raises(PathContainmentError):
            run_ledger_dir(sdd_dir, bad_id)
        with pytest.raises(PathContainmentError):
            admission_ledger_dir(sdd_dir, bad_id)

    assert [p.name for p in tmp_path.iterdir()] == [".sdd"]
    created = sorted(str(p.relative_to(sdd_dir)) for p in sdd_dir.rglob("*"))
    assert created == ["runtime", str(Path("runtime") / "ledger")]
