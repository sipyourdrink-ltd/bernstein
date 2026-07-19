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

import re
from pathlib import Path
from typing import Any

import pytest

from bernstein.core.admission.ledger import admission_ledger_dir
from bernstein.core.evidence.run_artifacts import _artifact_journal_path
from bernstein.core.persistence.work_ledger import (
    KIND_RUN_OPEN,
    LedgerReader,
    WorkLedger,
    default_ledger_root,
    run_ledger_dir,
    validated_canonical_lines,
)
from bernstein.core.replay.journal import EventJournal, JournalPathError
from bernstein.core.security.path_containment import (
    MAX_PATH_BYTES,
    MAX_SEGMENT_BYTES,
    PathContainmentError,
    PathTooLongError,
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


def test_max_segment_bytes_matches_posix_name_max() -> None:
    """The bound must be NAME_MAX, not merely "some large number".

    Widening it silently reintroduces the ENAMETOOLONG crash this bound was
    added to stop, so the value itself is the contract, not just the
    presence of a check.
    """
    assert MAX_SEGMENT_BYTES == 255


def test_segment_at_name_max_is_accepted_and_one_over_is_refused() -> None:
    """Pins the exact boundary, so widening or narrowing the bound fails."""
    assert validate_path_segment("A" * MAX_SEGMENT_BYTES) == "A" * MAX_SEGMENT_BYTES
    with pytest.raises(PathTooLongError):
        validate_path_segment("A" * (MAX_SEGMENT_BYTES + 1))


def test_derived_task_run_id_over_name_max_is_refused_by_the_bound() -> None:
    """The real derivation that motivated the bound trips it deterministically.

    ``_TASK_ID_RE`` accepts 256 characters and ``task_run_id`` adds a
    ``task-`` prefix, giving a 261-byte component. This asserts the barrier
    itself rejects it rather than relying on a filesystem probe, which
    returns False on macOS but raises ENAMETOOLONG on Linux.
    """
    from bernstein.core.tasks.checkpoint_retry import task_run_id

    segment = task_run_id("T" * 256)
    assert len(segment.encode()) > MAX_SEGMENT_BYTES
    with pytest.raises(PathTooLongError):
        validate_path_segment(segment)


def test_validate_path_segment_refuses_segment_over_name_max() -> None:
    """A component past NAME_MAX is a typed error, never an OSError.

    Without this the filesystem raises ``OSError(ENAMETOOLONG)`` at open(),
    which escapes the ``ValueError`` hierarchy every caller guards on and
    turns a rejected identifier into a crash. Linux raises; macOS returns
    False from ``is_file()``, so only the bound makes this deterministic
    across platforms.
    """
    with pytest.raises(PathTooLongError):
        validate_path_segment("A" * (MAX_SEGMENT_BYTES + 1))


def test_segment_length_is_measured_in_bytes_not_characters() -> None:
    """NAME_MAX is a byte limit, so a multi-byte name must not slip past.

    The alphabet rejects non-ASCII anyway, so this pins the bound itself
    rather than relying on the allowlist to be the only gate.
    """
    from bernstein.core.security.path_containment import MAX_SEGMENT_BYTES as limit

    two_byte = "é"  # one char, two bytes encoded
    assert len(two_byte) < len(two_byte.encode("utf-8"))
    with pytest.raises(PathContainmentError):
        validate_path_segment(two_byte * limit)


def test_contained_path_refuses_path_over_path_max(tmp_path: Path) -> None:
    """A legal-length segment under a deep base can still exceed PATH_MAX."""
    deep = tmp_path
    while len(str(deep).encode()) < MAX_PATH_BYTES:
        deep = deep / ("d" * 200)
    with pytest.raises(PathTooLongError):
        contained_path(deep, "run-1")


def test_path_too_long_is_a_containment_error_and_value_error() -> None:
    """The capacity error stays inside the documented exception hierarchy."""
    assert issubclass(PathTooLongError, PathContainmentError)
    assert issubclass(PathTooLongError, ValueError)


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
    _symlink_or_skip(root / "run-evil", outside)

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


# ---------------------------------------------------------------------------
# Every reader of a task journal, not just the artifact one
# ---------------------------------------------------------------------------


def _plant_symlinked_task_journal(tmp_path: Path) -> tuple[Path, Path]:
    """Point ``<sdd>/runs/task-T-1`` at an attacker-controlled journal.

    The planted chain is built with the real writer, so it verifies: a
    ``verify_journal`` recompute is unkeyed, and whoever can plant the
    symlink can also plant a self-consistent journal. Only containment
    distinguishes it from ours.
    """
    sdd_dir = tmp_path / ".sdd"
    runs_root = sdd_dir / "runs"
    runs_root.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    planted = EventJournal("planted", elsewhere)
    planted.record("checkpoint_recorded", task_id="T-1", checkpoint_id="cp-evil")
    _symlink_or_skip(runs_root / "task-T-1", planted.path.parent)
    return sdd_dir, planted.path


def test_task_journal_path_refuses_symlinked_run_dir(tmp_path: Path) -> None:
    """The shared helper every task-journal reader uses is contained."""
    from bernstein.core.tasks.checkpoint_retry import task_journal_path

    sdd_dir, _ = _plant_symlinked_task_journal(tmp_path)
    with pytest.raises(PathContainmentError):
        task_journal_path(sdd_dir, "T-1")


def test_progress_rows_refuse_symlinked_run_dir(tmp_path: Path) -> None:
    """The dashboard progress projection cannot read a planted journal."""
    from bernstein.core.replay import progress

    sdd_dir, _ = _plant_symlinked_task_journal(tmp_path)
    with pytest.raises(PathContainmentError):
        progress._load_task_journal_rows(sdd_dir, "T-1")


def test_latest_checkpoint_refuses_symlinked_run_dir(tmp_path: Path) -> None:
    """A planted checkpoint journal cannot fuel a warm resume."""
    from bernstein.core.tasks import checkpoint_retry

    sdd_dir, _ = _plant_symlinked_task_journal(tmp_path)
    with pytest.raises(PathContainmentError):
        checkpoint_retry.latest_checkpoint(sdd_dir, "T-1")


def test_suspension_journal_path_refuses_symlinked_run_dir(tmp_path: Path) -> None:
    """The suspension row reader resolves through the same barrier."""
    from bernstein.core.tasks import suspension

    sdd_dir, _ = _plant_symlinked_task_journal(tmp_path)
    with pytest.raises(PathContainmentError):
        suspension._journal_path(sdd_dir, "T-1")


def test_validated_canonical_lines_refuses_symlinked_bucket(tmp_path: Path) -> None:
    """The git-anchor export must not read a foreign file's bytes.

    ``validated_canonical_lines`` runs before the guarded reader in
    ``anchor_ledger``, so an unguarded read here would derive the anchored
    head hash from the symlink target.
    """
    real = tmp_path / "real"
    ledger = WorkLedger.open(real)
    ledger.append(kind=KIND_RUN_OPEN, payload={"goal": "private"})
    ledger.close()

    victim = tmp_path / "victim"
    victim.mkdir()
    _symlink_or_skip(victim / "000000.jsonl", real / "000000.jsonl", directory=False)

    with pytest.raises(PathContainmentError):
        validated_canonical_lines(victim)


def test_validated_canonical_lines_round_trips(tmp_path: Path) -> None:
    """An ordinary ledger still exports its validated lines and head."""
    ledger_dir = run_ledger_dir(tmp_path, "run-a")
    ledger = WorkLedger.open(ledger_dir)
    entry = ledger.append(kind=KIND_RUN_OPEN, payload={"goal": "ship it"})
    ledger.close()

    lines, head = validated_canonical_lines(ledger_dir)
    assert len(lines) == 1
    assert head == entry.entry_hash


# ---------------------------------------------------------------------------
# Run-journal readers (the run-level twin of the task-journal family)
# ---------------------------------------------------------------------------


def _plant_journal_outside_runs_root(tmp_path: Path) -> tuple[Path, str]:
    """Build a verifying journal outside ``<sdd>/runs`` and return a hop to it.

    The returned id is a relative traversal, the shape an operator can pass
    on the CLI. The planted chain is written by the real writer, so
    ``verify_journal`` recomputes it cleanly: only containment tells it
    apart from one of ours.
    """
    sdd_dir = tmp_path / ".sdd"
    (sdd_dir / "runs").mkdir(parents=True)
    other = tmp_path / "other_sdd"
    other.mkdir()
    planted = EventJournal("legit", other)
    planted.record("activity.result", stage="s1")
    return sdd_dir, "../../other_sdd/runs/legit"


def test_run_journal_path_refuses_traversal(tmp_path: Path) -> None:
    """The shared run-journal helper refuses a traversal id."""
    from bernstein.core.replay.journal import JournalPathError, run_journal_path

    sdd_dir, hostile = _plant_journal_outside_runs_root(tmp_path)
    with pytest.raises(JournalPathError):
        run_journal_path(sdd_dir, hostile)


def test_verify_run_activities_refuses_traversal_run_id(tmp_path: Path) -> None:
    """The activity verifier must not verify a journal outside the tree.

    Before routing, this returned ``found=True chain_ok=True`` for a journal
    read entirely outside the runs root, while ``EventJournal`` refused the
    byte-identical id.
    """
    from bernstein.core.orchestration.activity_modalities import verify_run_activities

    sdd_dir, hostile = _plant_journal_outside_runs_root(tmp_path)
    result = verify_run_activities(sdd_dir, run_id=hostile)
    assert result.found is False
    assert result.ok is False


def test_fork_run_refuses_traversal_run_id(tmp_path: Path) -> None:
    """Forking cannot seed a child run from a journal outside the tree.

    Matched on the containment branch specifically: an unrouted ``fork_run``
    also raises ``ForkError`` here, but only after reading the outside
    journal and failing to find a snapshot in it. A bare ``raises`` would
    pass either way and prove nothing.
    """
    from bernstein.core.replay.fork import ForkError, fork_run

    sdd_dir, hostile = _plant_journal_outside_runs_root(tmp_path)
    with pytest.raises(ForkError, match="cannot fork run"):
        fork_run(sdd_dir, hostile, from_step=0, repo_root=tmp_path)


def test_event_journal_still_refuses_the_same_id(tmp_path: Path) -> None:
    """The writer and the readers now agree on what a run id may name."""
    from bernstein.core.replay.journal import JournalPathError

    sdd_dir, hostile = _plant_journal_outside_runs_root(tmp_path)
    with pytest.raises(JournalPathError):
        EventJournal(hostile, sdd_dir)


# ---------------------------------------------------------------------------
# Allowlist edge cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_id", ["..\n", ".\n", "run-1\n"])
def test_validate_path_segment_refuses_trailing_newline(bad_id: str) -> None:
    """``$`` would accept a trailing newline; the allowlist anchors on ``\\Z``.

    ``"..\\n"`` is the sharp case: it is not equal to ``".."``, so the
    reserved-segment guard does not catch it either.
    """
    with pytest.raises(PathContainmentError):
        validate_path_segment(bad_id)


def test_long_task_id_reads_empty_rather_than_raising(tmp_path: Path) -> None:
    """An id the module's own validator blesses must not blow up a read.

    ``_TASK_ID_RE`` accepts 256 characters and ``task_run_id`` prefixes
    ``task-``, so the derived component exceeds NAME_MAX. That is a capacity
    failure, not an attack: the read degrades to "no journal" exactly as a
    missing file would, instead of raising ENAMETOOLONG out of the barrier.
    """
    from bernstein.core.evidence.run_artifacts import _validate_ids, read_artifact_rows

    sdd_dir = tmp_path / ".sdd"
    (sdd_dir / "runs").mkdir(parents=True)
    task_id = "T" * 256
    _validate_ids(task_id, "k")

    assert read_artifact_rows(sdd_dir, task_id) == []


def test_long_task_id_degrades_across_the_routed_readers(tmp_path: Path) -> None:
    """No routed reader turns an over-long id into a crash."""
    from bernstein.core.replay import progress
    from bernstein.core.tasks import checkpoint_retry

    sdd_dir = tmp_path / ".sdd"
    (sdd_dir / "runs").mkdir(parents=True)
    task_id = "T" * 256

    assert progress._load_task_journal_rows(sdd_dir, task_id) == []
    assert checkpoint_retry.latest_checkpoint(sdd_dir, task_id) is None


def test_containment_failure_is_never_swallowed_as_too_long(tmp_path: Path) -> None:
    """Readers degrade on capacity, never on a containment violation.

    The two failure modes share a base class, so this pins that the readers
    catch only the narrow one - a symlink escape must still surface.
    """
    from bernstein.core.evidence.run_artifacts import read_artifact_rows
    from bernstein.core.replay import progress
    from bernstein.core.tasks import checkpoint_retry

    sdd_dir, _ = _plant_symlinked_task_journal(tmp_path)

    for call in (
        lambda: read_artifact_rows(sdd_dir, "T-1"),
        lambda: progress._load_task_journal_rows(sdd_dir, "T-1"),
        lambda: checkpoint_retry.latest_checkpoint(sdd_dir, "T-1"),
    ):
        with pytest.raises(PathContainmentError) as caught:
            call()
        assert not isinstance(caught.value, PathTooLongError)


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


# ---------------------------------------------------------------------------
# Sink closure: the set of journal readers must stay routed
# ---------------------------------------------------------------------------
#
# The barrier tests above prove the barrier works. These prove the ROUTING:
# that every reader actually goes through it. Without them a new call site
# can join a journal path by hand and silently reopen the escape, which is
# exactly what happened twice on this branch.


def _source_files() -> list[Path]:
    src = Path(__file__).resolve().parents[2] / "src" / "bernstein"
    return sorted(src.rglob("*.py"))


#: Helpers that ARE the barrier, so a journal filename on their own lines is
#: the routed construction rather than a bypass of it.
_BARRIER_CALLS = (
    "contained_path(",
    "run_journal_path(",
    "task_journal_path(",
    "contained_run_journal(",
)


def test_no_unrouted_journal_path_construction() -> None:
    """No source file may build a journal path outside the barrier.

    The escape this PR closes is reachable from any raw
    ``<base> / <name> / journal.jsonl`` join, so the guarantee is only as
    good as the claim that no such join remains. This asserts that claim
    mechanically instead of trusting a hand-kept list: a new unrouted reader
    fails here the moment it is added.

    There is deliberately no allowlist. Sites that obtain the directory name
    by iterating the runs root are covered too: iteration proves the name is
    a single innocent component, but an entry with an ordinary name can
    still be a symlink pointing outside the root, and only resolution
    catches that. An exemption here would cover exactly the half of the
    threat iteration already handles.
    """
    # The word boundary goes INSIDE each alternative: a trailing \b after
    # the quoted literal can never match, because the pattern ends on a
    # quote and \b there demands a following word character. That hole made
    # every `x / "journal.jsonl"` form invisible to this scan.
    pattern = re.compile(r"/\s*(?:JOURNAL_FILENAME\b|\"journal\.jsonl\"|_REPLAY_JSONL\b)")
    offenders: list[str] = []
    for path in _source_files():
        rel = path.relative_to(path.parents[1]).as_posix()
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not pattern.search(line):
                continue
            if any(call in line for call in _BARRIER_CALLS):
                continue
            offenders.append(f"{rel}:{lineno}: {line.strip()}")

    assert not offenders, (
        "these sites build a journal path without the containment barrier; "
        "route them through run_journal_path / task_journal_path / "
        "contained_run_journal / contained_path:\n  " + "\n  ".join(offenders)
    )


def test_every_run_journal_reader_refuses_a_planted_journal(tmp_path: Path) -> None:
    """Enumerate the run-journal readers; each must refuse a planted journal.

    One planted symlink, every reader. A reader that starts following it
    again fails here even if its own dedicated test is removed.
    """
    from bernstein.cli.commands import thread_cmd
    from bernstein.core.orchestration import escalation
    from bernstein.core.orchestration.activity_modalities import verify_run_activities
    from bernstein.core.replay import review_board
    from bernstein.core.replay.fork import ForkError, fork_run
    from bernstein.core.replay.journal import run_journal_path

    sdd_dir = tmp_path / ".sdd"
    runs_root = sdd_dir / "runs"
    runs_root.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    planted = EventJournal("planted", elsewhere)
    planted.record("activity.result", stage="s1")
    _symlink_or_skip(runs_root / "planted", planted.path.parent)
    assert planted.path.is_file()

    # Each entry: (name, call, predicate that the planted journal was refused).
    checks: list[tuple[str, Any]] = [
        ("run_journal_path", lambda: run_journal_path(sdd_dir, "planted")),
        ("escalation._journal_path", lambda: escalation._journal_path(sdd_dir, "planted")),
        ("review_board.project_run", lambda: review_board.project_run(sdd_dir, "planted")),
        ("thread_cmd.thread_verify", lambda: thread_cmd.thread_verify(run_id="planted", sdd_dir=sdd_dir, as_json=True)),
        ("verify_run_activities", lambda: verify_run_activities(sdd_dir, run_id="planted")),
        ("fork_run", lambda: fork_run(sdd_dir, "planted", from_step=0, repo_root=tmp_path)),
    ]

    followed: list[str] = []
    for name, call in checks:
        try:
            result = call()
        except (PathContainmentError, ForkError):
            continue
        # A reader may report rather than raise, but it must not report a
        # pass or hand back a projection built from the planted journal.
        if name == "review_board.project_run" and result is None:
            continue
        if name == "thread_cmd.thread_verify" and result != 0:
            continue
        if name == "verify_run_activities" and not result.found:
            continue
        followed.append(f"{name} -> {result!r}")

    assert not followed, "these readers followed a journal planted outside the runs root:\n  " + "\n  ".join(followed)


# ---------------------------------------------------------------------------
# A verifier reports; it does not raise
# ---------------------------------------------------------------------------


def _tampered_audit_chain(root: Path) -> None:
    """Build a real audit chain under *root* and tamper one record in place."""
    from bernstein.core.security.audit_chain import AuditChainStore

    audit_dir = root / ".sdd" / "audit"
    audit_dir.mkdir(parents=True)
    chain = AuditChainStore(audit_dir)
    for transition in ("submitted", "done"):
        chain.log(
            event_type="run.lifecycle",
            resource_type="run",
            resource_id="r",
            details={"run_id": "r", "transition": transition},
            actor="tester",
        )
    log_file = sorted(audit_dir.glob("*.jsonl"))[0]
    lines = log_file.read_text(encoding="utf-8").splitlines()
    lines[1] = lines[1].replace("done", "TAMPERED")
    log_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.mark.parametrize("bad_run_id", ["r:1", "..", "", "../escape"])
def test_verify_run_reports_an_unusable_run_id_without_losing_the_audit_finding(
    tmp_path: Path,
    bad_run_id: str,
) -> None:
    """An identifier check must not destroy a computed audit-tamper finding.

    ``verify_run`` verifies the audit chain first and the ledger second. If
    the ledger step raises on a bad run id, the tamper finding it already
    computed is thrown away and the operator gets a traceback instead of the
    verdict - strictly worse than not having checked at all.
    """
    from bernstein.core.run_service.verify import verify_run

    _tampered_audit_chain(tmp_path)

    result = verify_run(tmp_path, bad_run_id)

    assert result.audit_ok is False, "the audit-tamper finding must survive"
    assert any(e.startswith("audit:") for e in result.errors)
    assert result.ledger_ok is False
    assert any("unusable run id" in e for e in result.errors)
    assert result.ok is False


# ---------------------------------------------------------------------------
# Sweeps: iteration gives an innocent NAME, not an innocent TARGET
# ---------------------------------------------------------------------------


def test_sweeps_skip_a_symlinked_run_directory_with_an_innocent_name(tmp_path: Path) -> None:
    """A run directory whose name is ordinary but which points outside is skipped.

    Iterating the runs root proves the entry name is a single component with
    no ``..`` and no separator. It proves nothing about what the entry
    resolves to. This plants a directory called ``run-ok`` - an entirely
    legitimate name - that is a symlink to a tree outside the runs root, and
    asserts every sweeping reader declines to read through it.
    """
    from bernstein.cli.commands.advanced_cmd import _replay_find_run_dirs
    from bernstein.core.evidence.run_artifacts import live_artifact_content_hashes
    from bernstein.core.replay.review_board import list_board_runs

    sdd_dir = tmp_path / ".sdd"
    runs_root = sdd_dir / "runs"
    runs_root.mkdir(parents=True)

    # A genuine run inside the root, so the sweeps have real work to do.
    honest = EventJournal("run-honest", sdd_dir)
    honest.record("tool.call", tool="Read")

    # A planted run outside the root, reachable only through the symlink.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    planted = EventJournal("planted", elsewhere)
    planted.record("artifact_posted", task_id="T-1", key="k", content_hash="sha256:evil")
    _symlink_or_skip(runs_root / "run-ok", planted.path.parent)

    assert (runs_root / "run-ok").is_dir(), "symlink should look like an ordinary run dir"
    assert (runs_root / "run-ok" / "journal.jsonl").is_file(), "and its journal should look readable"

    assert "run-ok" not in list_board_runs(sdd_dir)
    assert "run-honest" in list_board_runs(sdd_dir)

    assert "sha256:evil" not in live_artifact_content_hashes(sdd_dir)

    replay_dirs = {d.name for d in _replay_find_run_dirs(runs_root)}
    assert "run-ok" not in replay_dirs
    assert "run-honest" in replay_dirs
