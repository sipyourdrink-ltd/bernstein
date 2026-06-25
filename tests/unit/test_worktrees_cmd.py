"""Unit tests for the ``bernstein worktrees`` subcommand and classifier.

Every fixture builds an isolated ``.sdd/`` tree under ``tmp_path`` - the
suite NEVER touches the real repo's 30+ worktrees.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.worktrees_cmd import (
    GcLockError,
    format_age,
    lock_gc,
    render_worktrees_table,
    run_gc,
    worktrees_group,
)
from bernstein.core.security.audit import AuditLog
from bernstein.core.worktrees.classifier import (
    GC_LOCK_RELPATH,
    STALE_TRACE_AGE_S,
    WORKTREE_REAP_EVENT,
    ClassifiedWorktree,
    WorktreeState,
    classify_worktrees,
    format_size,
    iter_worktree_dirs,
    reap_worktree,
    worktree_fingerprint,
    worktrees_root,
)

#: 32-byte HMAC key injected into every test AuditLog so the suite never
#: touches the operator's real signing key.
_AUDIT_KEY = b"worktree-reap-test-key-32-bytes!"

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _init_repo(repo_root: Path) -> None:
    """Initialise a bare git repo with one main commit at ``repo_root``."""
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo_root)], check=True)
    (repo_root / "seed.txt").write_text("seed")
    subprocess.run(["git", "-C", str(repo_root), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "-c",
            "user.email=test@bernstein",
            "-c",
            "user.name=test",
            "commit",
            "-q",
            "-m",
            "seed",
        ],
        check=True,
    )


def _make_worktree_dir(repo_root: Path, session_id: str, *, with_git: bool = True) -> Path:
    """Create a worktree directory under ``.sdd/runtime/worktrees``.

    With ``with_git=True`` (the default) this creates a *real* git worktree
    via ``git worktree add`` on a fresh ``agent/<session>`` branch off
    ``main``. A real worktree is required because the classifier now probes
    git state (``git status --porcelain`` and merge-ancestry) to refuse
    reaping worktrees that hold unsaved work; a fake ``.git`` stub cannot be
    probed and is conservatively preserved. The fresh worktree is clean and
    fully merged, so it classifies as reapable - matching the common case
    the GC was built for.

    With ``with_git=False`` it creates a plain directory (no ``.git``
    anchor) holding one file, which the classifier marks ``CORRUPT``.
    """
    base = repo_root / ".sdd" / "runtime" / "worktrees"
    base.mkdir(parents=True, exist_ok=True)
    wt = base / session_id
    if with_git:
        subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "-c",
                "user.email=test@bernstein",
                "-c",
                "user.name=test",
                "-c",
                "commit.gpgsign=false",
                "worktree",
                "add",
                "-q",
                "-b",
                f"agent/{session_id}",
                str(wt),
                "main",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return wt
    wt.mkdir()
    (wt / "file.txt").write_text("hello")
    return wt


def _write_pid_record(repo_root: Path, session_id: str, *, pid: int, task_id: str | None = None) -> None:
    pid_dir = repo_root / ".sdd" / "runtime" / "pids"
    pid_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {"worker_pid": pid}
    if task_id is not None:
        payload["task_id"] = task_id
    (pid_dir / f"{session_id}.json").write_text(json.dumps(payload))


def _write_trace(repo_root: Path, session_id: str, *, mtime: float | None = None) -> Path:
    trace_dir = repo_root / ".sdd" / "traces"
    trace_dir.mkdir(parents=True, exist_ok=True)
    path = trace_dir / f"{session_id}.jsonl"
    path.write_text("{}\n")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


@pytest.fixture()
def repo_root(tmp_path: Path) -> Path:
    """Initialised throw-away repo root."""
    _init_repo(tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# Classifier - 4-state matrix
# ---------------------------------------------------------------------------


def test_classifier_active(repo_root: Path) -> None:
    """Live PID + task record + recent trace => active."""
    sid = "alive"
    _make_worktree_dir(repo_root, sid)
    _write_pid_record(repo_root, sid, pid=os.getpid(), task_id="task-1")
    _write_trace(repo_root, sid)

    rows = classify_worktrees(repo_root)
    assert len(rows) == 1
    assert rows[0].state is WorktreeState.ACTIVE
    assert rows[0].task_id == "task-1"
    assert rows[0].pid_alive is True


def test_classifier_orphan(repo_root: Path) -> None:
    """Directory exists but no PID record => orphan."""
    sid = "orph"
    _make_worktree_dir(repo_root, sid)

    rows = classify_worktrees(repo_root)
    assert rows[0].state is WorktreeState.ORPHAN
    assert rows[0].is_reapable is True


def test_classifier_stale(repo_root: Path) -> None:
    """Dead PID + trace older than 24h => stale."""
    sid = "stale"
    _make_worktree_dir(repo_root, sid)
    # Use a PID very unlikely to be in use.
    dead_pid = 999_999_998
    _write_pid_record(repo_root, sid, pid=dead_pid, task_id="task-stale")
    long_ago = time.time() - (STALE_TRACE_AGE_S + 3600)
    _write_trace(repo_root, sid, mtime=long_ago)

    rows = classify_worktrees(repo_root)
    assert rows[0].state is WorktreeState.STALE
    assert rows[0].is_reapable is True


def test_classifier_corrupt(repo_root: Path) -> None:
    """Directory has no .git anchor => corrupt.

    A corrupt directory cannot be probed with git. When it still holds
    files (the helper writes ``file.txt``) we cannot prove it is free of
    unsaved work, so it is preserved for manual handling rather than reaped.
    """
    sid = "corrupt"
    _make_worktree_dir(repo_root, sid, with_git=False)

    rows = classify_worktrees(repo_root)
    assert rows[0].state is WorktreeState.CORRUPT
    assert rows[0].has_unsaved_work is True
    assert rows[0].is_reapable is False


def test_classifier_corrupt_empty_is_reapable(repo_root: Path) -> None:
    """An empty corrupt directory (no tracked content) still reaps."""
    base = repo_root / ".sdd" / "runtime" / "worktrees"
    base.mkdir(parents=True, exist_ok=True)
    (base / "corrupt-empty").mkdir()

    rows = classify_worktrees(repo_root)
    assert rows[0].state is WorktreeState.CORRUPT
    assert rows[0].has_unsaved_work is False
    assert rows[0].is_reapable is True


def test_classifier_dead_pid_recent_trace_stays_active(repo_root: Path) -> None:
    """Dead PID but recent trace => still active (avoid racing).

    We refuse to reap a worktree whose trace is fresh, even if its PID
    has gone away - the agent may simply be restarting.
    """
    sid = "race"
    _make_worktree_dir(repo_root, sid)
    _write_pid_record(repo_root, sid, pid=999_999_997, task_id="task-race")
    _write_trace(repo_root, sid)  # mtime = now

    rows = classify_worktrees(repo_root)
    assert rows[0].state is WorktreeState.ACTIVE
    assert rows[0].is_reapable is False


def test_iter_worktree_dirs_skips_locks(repo_root: Path) -> None:
    """The bookkeeping ``.locks`` directory is excluded from iteration."""
    _make_worktree_dir(repo_root, "real")
    locks = worktrees_root(repo_root) / ".locks"
    locks.mkdir()
    (locks / "foo.lock").write_text("")
    assert [p.name for p in iter_worktree_dirs(repo_root)] == ["real"]


# ---------------------------------------------------------------------------
# Lock - concurrency safety
# ---------------------------------------------------------------------------


def test_lock_prevents_concurrent_gc(repo_root: Path) -> None:
    """A second ``lock_gc`` while the first is held raises ``GcLockError``."""
    with lock_gc(repo_root):
        with pytest.raises(GcLockError):
            with lock_gc(repo_root):
                pass


def test_lock_released_on_exception(repo_root: Path) -> None:
    """The lock file is unlinked even when the body raises."""

    class Boom(RuntimeError):
        pass

    with pytest.raises(Boom):
        with lock_gc(repo_root):
            raise Boom

    # Lock should be gone - next acquisition succeeds.
    with lock_gc(repo_root):
        pass


def test_lock_held_by_thread_blocks_second(repo_root: Path) -> None:
    """Concurrent threads must serialise through the lock file."""
    started = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []

    def hold() -> None:
        try:
            with lock_gc(repo_root):
                started.set()
                release.wait(timeout=5)
        except BaseException as exc:  # pragma: no cover - propagated below
            errors.append(exc)

    holder = threading.Thread(target=hold)
    holder.start()
    started.wait(timeout=5)
    try:
        with pytest.raises(GcLockError):
            with lock_gc(repo_root):
                pass
    finally:
        release.set()
        holder.join(timeout=5)
    assert not errors


# ---------------------------------------------------------------------------
# Reap behaviour - --dry and real deletion
# ---------------------------------------------------------------------------


def _orphan_row(repo_root: Path, sid: str) -> ClassifiedWorktree:
    """Convenience: classify a single orphan worktree."""
    rows = classify_worktrees(repo_root)
    matching = [r for r in rows if r.session_id == sid]
    assert matching, f"no row for {sid}"
    return matching[0]


def test_reap_worktree_dry_run_does_not_touch_disk(repo_root: Path) -> None:
    """``dry_run=True`` leaves the directory in place."""
    sid = "dry"
    wt = _make_worktree_dir(repo_root, sid)
    row = _orphan_row(repo_root, sid)
    assert row.state is WorktreeState.ORPHAN

    assert reap_worktree(repo_root, row, dry_run=True) is True
    assert wt.exists()
    # The real worktree checks out main's seed file; dry-run leaves it intact.
    assert (wt / "seed.txt").read_text() == "seed"


def test_reap_worktree_real_removes_directory(repo_root: Path) -> None:
    """Without ``dry_run`` the directory is gone after the call."""
    sid = "real"
    wt = _make_worktree_dir(repo_root, sid)
    row = _orphan_row(repo_root, sid)

    assert reap_worktree(repo_root, row, dry_run=False) is True
    assert not wt.exists()


def test_run_gc_invokes_git_prune(repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``run_gc`` runs ``git worktree prune`` after each reap."""
    sid = "pruned"
    _make_worktree_dir(repo_root, sid)
    row = _orphan_row(repo_root, sid)

    captured: list[list[str]] = []
    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        if isinstance(cmd, list) and cmd[:3] == ["git", "worktree", "prune"]:
            captured.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)

    run_gc(repo_root, [row], dry_run=False)
    assert any(c[:3] == ["git", "worktree", "prune"] for c in captured)


def test_run_gc_dry_run_skips_prune(repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--dry`` must not invoke ``git worktree prune`` either."""
    sid = "dry-prune"
    _make_worktree_dir(repo_root, sid)
    row = _orphan_row(repo_root, sid)

    captured: list[list[str]] = []
    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        if isinstance(cmd, list) and cmd[:3] == ["git", "worktree", "prune"]:
            captured.append(list(cmd))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)

    run_gc(repo_root, [row], dry_run=True)
    assert captured == []
    assert row.path.exists()  # filesystem untouched


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def test_format_age_buckets() -> None:
    assert format_age(0) == "0s"
    assert format_age(59) == "59s"
    assert format_age(60) == "1m"
    assert format_age(3600) == "1h 00m"
    assert format_age(86_400) == "1d 00h"
    assert format_age(86_400 + 7200) == "1d 02h"


def test_format_size_units() -> None:
    assert format_size(0) == "0 B"
    assert format_size(1023) == "1023 B"
    assert format_size(1024) == "1.0 KB"
    assert format_size(1024 * 1024) == "1.0 MB"


def test_render_table_has_columns() -> None:
    """Smoke test that the table builder accepts classifier output."""
    row = ClassifiedWorktree(
        path=Path("/tmp/x"),
        session_id="x",
        task_id=None,
        state=WorktreeState.ORPHAN,
        age_seconds=300,
        size_bytes=2048,
        pid=None,
        pid_alive=False,
        last_trace_mtime=None,
    )
    table = render_worktrees_table([row])
    headers = [col.header for col in table.columns]
    assert headers == ["Path", "Task", "State", "Age", "Size", "PID"]


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


def test_cli_list_renders_table(repo_root: Path) -> None:
    """``worktrees list`` prints a table with one row per worktree."""
    _make_worktree_dir(repo_root, "alpha")
    _make_worktree_dir(repo_root, "beta")
    runner = CliRunner()
    result = runner.invoke(worktrees_group, ["list", "--workdir", str(repo_root)])
    assert result.exit_code == 0, result.output
    assert "alpha" in result.output
    assert "beta" in result.output
    assert "orphan" in result.output


def test_cli_list_json_output(repo_root: Path) -> None:
    """``--json`` returns a parseable JSON array."""
    _make_worktree_dir(repo_root, "alpha")
    runner = CliRunner()
    result = runner.invoke(worktrees_group, ["list", "--workdir", str(repo_root), "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert len(payload) == 1
    assert payload[0]["state"] == "orphan"
    assert payload[0]["reapable"] is True


def test_cli_gc_dry_run_keeps_disk(repo_root: Path) -> None:
    """``gc --dry --yes`` reports work but never deletes."""
    wt = _make_worktree_dir(repo_root, "gc-dry")
    runner = CliRunner()
    result = runner.invoke(
        worktrees_group,
        ["gc", "--workdir", str(repo_root), "--yes", "--dry"],
    )
    assert result.exit_code == 0, result.output
    assert wt.exists()
    assert "Would remove" in result.output


def test_cli_gc_yes_deletes(repo_root: Path) -> None:
    """``gc --yes`` skips the prompt and removes orphans."""
    wt = _make_worktree_dir(repo_root, "gc-real")
    runner = CliRunner()
    result = runner.invoke(worktrees_group, ["gc", "--workdir", str(repo_root), "--yes"])
    assert result.exit_code == 0, result.output
    assert not wt.exists()


def test_cli_gc_no_reapable(repo_root: Path) -> None:
    """When everything is active, ``gc`` is a no-op with friendly output."""
    sid = "live"
    _make_worktree_dir(repo_root, sid)
    _write_pid_record(repo_root, sid, pid=os.getpid(), task_id="t")
    _write_trace(repo_root, sid)
    runner = CliRunner()
    result = runner.invoke(worktrees_group, ["gc", "--workdir", str(repo_root), "--yes"])
    assert result.exit_code == 0
    assert "nothing to do" in result.output.lower()


def test_cli_gc_concurrent_lock_collision(repo_root: Path) -> None:
    """Holding the lock externally makes ``gc`` exit with code 2."""
    lock_path = repo_root / GC_LOCK_RELPATH
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("{}")
    _make_worktree_dir(repo_root, "blocked")

    runner = CliRunner()
    result = runner.invoke(worktrees_group, ["gc", "--workdir", str(repo_root), "--yes"])
    assert result.exit_code == 2
    assert "already running" in result.output.lower()


# ---------------------------------------------------------------------------
# Audit anchoring (issue #1833) - reaps recorded in the HMAC chain
# ---------------------------------------------------------------------------


def _audit_log(repo_root: Path) -> AuditLog:
    """Return an AuditLog rooted at the project's ``.sdd/audit`` dir.

    Always injects a fixed test key so verification is deterministic and the
    operator's real signing key is never read or created.
    """
    return AuditLog(audit_dir=repo_root / ".sdd" / "audit", key=_AUDIT_KEY)


def _add_real_worktree(repo_root: Path, session_id: str, *, dirty: bool = False) -> Path:
    """Create a genuine ``git worktree`` so HEAD/dirty capture has real data.

    Unlike :func:`_make_worktree_dir`, this registers the directory with git
    so ``worktree_fingerprint`` can read a real HEAD sha. The worktree is
    placed under ``.sdd/runtime/worktrees`` exactly like a Bernstein agent
    worktree so the classifier picks it up.
    """
    base = repo_root / ".sdd" / "runtime" / "worktrees"
    base.mkdir(parents=True, exist_ok=True)
    wt = base / session_id
    subprocess.run(
        ["git", "-C", str(repo_root), "worktree", "add", "-q", "--detach", str(wt)],
        check=True,
    )
    if dirty:
        (wt / "scratch.txt").write_text("uncommitted change")
    return wt


def test_worktree_fingerprint_reads_head_and_clean_flag(repo_root: Path) -> None:
    """A real, clean worktree fingerprints to its HEAD sha + dirty=False."""
    wt = _add_real_worktree(repo_root, "fp-clean")
    expected = subprocess.run(
        ["git", "-C", str(wt), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    fp = worktree_fingerprint(wt)
    assert fp.head_sha == expected
    assert fp.dirty is False


def test_worktree_fingerprint_flags_dirty_tree(repo_root: Path) -> None:
    """An uncommitted file makes the fingerprint report dirty=True."""
    wt = _add_real_worktree(repo_root, "fp-dirty", dirty=True)
    fp = worktree_fingerprint(wt)
    assert fp.dirty is True
    assert fp.head_sha is not None


def test_worktree_fingerprint_corrupt_degrades_to_unknown(repo_root: Path) -> None:
    """A directory with no readable ``.git`` degrades, never crashes."""
    wt = _make_worktree_dir(repo_root, "fp-corrupt", with_git=False)
    fp = worktree_fingerprint(wt)
    assert fp.head_sha is None
    assert fp.dirty is None


def test_run_gc_writes_one_reap_event_with_expected_fields(repo_root: Path) -> None:
    """A reap appends exactly one ``worktree.reap`` event with all fields.

    The injected AuditLog must verify cleanly afterwards.
    """
    sid = "audited"
    _make_worktree_dir(repo_root, sid)
    row = _orphan_row(repo_root, sid)
    log = _audit_log(repo_root)

    removed = run_gc(repo_root, [row], dry_run=False, audit_log=log)
    assert removed == 1

    events = log.query(event_type=WORKTREE_REAP_EVENT)
    assert len(events) == 1
    ev = events[0]
    assert ev.resource_type == "worktree"
    assert ev.resource_id == sid
    d = ev.details
    assert d["state"] == "orphan"
    assert d["task_id"] is None
    assert d["path"] == str(row.path)
    assert d["size_bytes"] == row.size_bytes
    assert d["dry_run"] is False
    # Fingerprint + classifier metadata are present (corrupt-degrade allowed
    # for head_sha, but the keys must exist for forensic reconstruction).
    assert "head_sha" in d
    assert "dirty" in d
    assert "age_seconds" in d
    assert "last_trace_mtime" in d

    valid, errors = log.verify()
    assert valid is True
    assert errors == []


def test_run_gc_records_pre_deletion_head_and_dirty(repo_root: Path) -> None:
    """The reap event proves the pre-deletion git HEAD sha and dirty flag."""
    sid = "fp-recorded"
    wt = _add_real_worktree(repo_root, sid, dirty=True)
    expected_head = subprocess.run(
        ["git", "-C", str(wt), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    row = _orphan_row(repo_root, sid)
    log = _audit_log(repo_root)

    run_gc(repo_root, [row], dry_run=False, audit_log=log)

    ev = log.query(event_type=WORKTREE_REAP_EVENT)[0]
    assert ev.details["head_sha"] == expected_head
    assert ev.details["dirty"] is True
    assert not wt.exists()  # the worktree really was reaped


def test_run_gc_dry_run_flags_event_and_keeps_disk(repo_root: Path) -> None:
    """``--dry`` records the event flagged ``dry_run=true`` and deletes nothing."""
    sid = "dry-audited"
    wt = _make_worktree_dir(repo_root, sid)
    row = _orphan_row(repo_root, sid)
    log = _audit_log(repo_root)

    removed = run_gc(repo_root, [row], dry_run=True, audit_log=log)
    assert removed == 0
    assert wt.exists()  # nothing destroyed

    events = log.query(event_type=WORKTREE_REAP_EVENT)
    assert len(events) == 1
    assert events[0].details["dry_run"] is True
    valid, _ = log.verify()
    assert valid is True


def test_run_gc_fail_closed_when_audit_append_raises(repo_root: Path) -> None:
    """If the audit append fails the worktree must NOT be reaped (fail-closed)."""
    sid = "fail-closed"
    wt = _make_worktree_dir(repo_root, sid)
    row = _orphan_row(repo_root, sid)

    class _ExplodingLog:
        def log(self, *args: object, **kwargs: object) -> None:
            raise OSError("simulated full disk / key permission error")

    with pytest.raises(OSError, match="simulated full disk"):
        run_gc(repo_root, [row], dry_run=False, audit_log=_ExplodingLog())  # type: ignore[arg-type]

    # Fail-closed contract: the directory survives because the reap was aborted.
    assert wt.exists()


def test_run_gc_writes_event_without_hook_registry(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The audit event is written even when no plugin HookRegistry exists.

    We force the lifecycle bridge to be unavailable; the audit append must
    still happen (the trail does not depend on the lifecycle hook).
    """
    sid = "no-registry"
    _make_worktree_dir(repo_root, sid)
    row = _orphan_row(repo_root, sid)
    log = _audit_log(repo_root)

    import bernstein.cli.commands.worktrees_cmd as wt_cmd

    monkeypatch.setattr(wt_cmd, "_shared_registry", lambda: None)

    run_gc(repo_root, [row], dry_run=False, audit_log=log)
    assert len(log.query(event_type=WORKTREE_REAP_EVENT)) == 1


def test_run_gc_corrupt_worktree_records_unknown_head(repo_root: Path) -> None:
    """A corrupt worktree (no .git) is reaped with head_sha=null, no crash."""
    sid = "corrupt-audited"
    wt = _make_worktree_dir(repo_root, sid, with_git=False)
    row = _orphan_row(repo_root, sid)
    assert row.state is WorktreeState.CORRUPT
    log = _audit_log(repo_root)

    run_gc(repo_root, [row], dry_run=False, audit_log=log)
    assert not wt.exists()

    ev = log.query(event_type=WORKTREE_REAP_EVENT)[0]
    assert ev.details["state"] == "corrupt"
    assert ev.details["head_sha"] is None
    assert ev.details["dirty"] is None
    valid, _ = log.verify()
    assert valid is True


def test_run_gc_default_audit_log_targets_sdd_audit(repo_root: Path) -> None:
    """With no injected log, run_gc writes to ``<repo>/.sdd/audit`` itself.

    Uses a per-test key path so the operator's real key is untouched.
    """
    sid = "default-log"
    _make_worktree_dir(repo_root, sid)
    row = _orphan_row(repo_root, sid)

    key_path = repo_root / "audit.key"
    key_path.write_bytes(_AUDIT_KEY)
    key_path.chmod(0o600)
    os.environ["BERNSTEIN_AUDIT_KEY_PATH"] = str(key_path)
    try:
        run_gc(repo_root, [row], dry_run=False)
    finally:
        os.environ.pop("BERNSTEIN_AUDIT_KEY_PATH", None)

    audit_dir = repo_root / ".sdd" / "audit"
    assert audit_dir.is_dir()
    log = AuditLog(audit_dir=audit_dir, key=_AUDIT_KEY)
    assert len(log.query(event_type=WORKTREE_REAP_EVENT)) == 1


# ---------------------------------------------------------------------------
# CLI + negative integration (issue #1833 acceptance criteria)
# ---------------------------------------------------------------------------


def test_cli_gc_yes_writes_reap_events_and_verifies(repo_root: Path) -> None:
    """``gc --yes`` over two reapable worktrees writes 2 verifiable events.

    Mirrors the issue integration AC: after the sweep the chain verifies and
    contains exactly N ``worktree.reap`` rows. We pin the audit key via env so
    the CLI's own AuditLog is reproducible.
    """
    _make_worktree_dir(repo_root, "sweep-a")
    _make_worktree_dir(repo_root, "sweep-b")

    key_path = repo_root / "audit.key"
    key_path.write_bytes(_AUDIT_KEY)
    key_path.chmod(0o600)
    os.environ["BERNSTEIN_AUDIT_KEY_PATH"] = str(key_path)
    try:
        runner = CliRunner()
        result = runner.invoke(worktrees_group, ["gc", "--workdir", str(repo_root), "--yes"])
        assert result.exit_code == 0, result.output
    finally:
        os.environ.pop("BERNSTEIN_AUDIT_KEY_PATH", None)

    log = AuditLog(audit_dir=repo_root / ".sdd" / "audit", key=_AUDIT_KEY)
    events = log.query(event_type=WORKTREE_REAP_EVENT)
    assert len(events) == 2
    valid, errors = log.verify()
    assert valid is True, errors


def test_tampering_with_recorded_head_breaks_verify(repo_root: Path) -> None:
    """Editing a recorded HEAD sha in the JSONL trips an HMAC mismatch."""
    sid = "tamper"
    _add_real_worktree(repo_root, sid)
    row = _orphan_row(repo_root, sid)
    log = _audit_log(repo_root)
    run_gc(repo_root, [row], dry_run=False, audit_log=log)

    # Sanity: clean chain before tampering.
    assert log.verify()[0] is True

    audit_dir = repo_root / ".sdd" / "audit"
    jsonl = sorted(audit_dir.glob("*.jsonl"))[0]
    lines = jsonl.read_text().splitlines()
    reap_idx = next(i for i, ln in enumerate(lines) if WORKTREE_REAP_EVENT in ln)
    entry = json.loads(lines[reap_idx])
    # Flip one hex char of the recorded HEAD sha (keep it valid JSON).
    original = entry["details"]["head_sha"]
    flipped = ("b" if original[0] != "b" else "a") + original[1:]
    entry["details"]["head_sha"] = flipped
    lines[reap_idx] = json.dumps(entry, sort_keys=True)
    jsonl.write_text("\n".join(lines) + "\n")

    fresh = AuditLog(audit_dir=audit_dir, key=_AUDIT_KEY)
    valid, errors = fresh.verify()
    assert valid is False
    assert any("HMAC mismatch" in e or "non-canonical" in e for e in errors)


# ----------------------------------------------------------------------------
# GC lock stale recovery (gc-reliability): a crashed GC must not wedge future runs
# ----------------------------------------------------------------------------


def test_gc_lock_stale_when_pid_dead() -> None:
    import time as _time
    from unittest.mock import patch

    from bernstein.cli.commands.worktrees_cmd import _gc_lock_is_stale

    with patch("bernstein.cli.commands.worktrees_cmd.is_process_alive", return_value=False):
        assert _gc_lock_is_stale({"pid": 999999, "started_at": _time.time()}) is True


def test_gc_lock_not_stale_when_pid_alive() -> None:
    import os as _os
    import time as _time
    from unittest.mock import patch

    from bernstein.cli.commands.worktrees_cmd import _gc_lock_is_stale

    with patch("bernstein.cli.commands.worktrees_cmd.is_process_alive", return_value=True):
        assert _gc_lock_is_stale({"pid": _os.getpid(), "started_at": _time.time()}) is False


def test_gc_lock_stale_when_too_old() -> None:
    import os as _os
    import time as _time
    from unittest.mock import patch

    from bernstein.cli.commands.worktrees_cmd import _gc_lock_is_stale

    with patch("bernstein.cli.commands.worktrees_cmd.is_process_alive", return_value=True):
        assert _gc_lock_is_stale({"pid": _os.getpid(), "started_at": _time.time() - 7 * 3600}) is True


def test_gc_lock_unreadable_is_not_stale() -> None:
    from bernstein.cli.commands.worktrees_cmd import _gc_lock_is_stale

    assert _gc_lock_is_stale(None) is False


def test_lock_gc_reclaims_lock_from_dead_owner(repo_root: Path) -> None:
    """A lock left by a crashed GC (dead pid) is reclaimed, not wedged forever."""
    import json as _json
    import time as _time
    from unittest.mock import patch

    from bernstein.cli.commands.worktrees_cmd import GC_LOCK_RELPATH

    lock_path = repo_root / GC_LOCK_RELPATH
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(_json.dumps({"pid": 999999, "started_at": _time.time()}), encoding="utf-8")

    with patch("bernstein.cli.commands.worktrees_cmd.is_process_alive", return_value=False):
        with lock_gc(repo_root):
            assert lock_path.exists()
    assert not lock_path.exists()


def test_lock_gc_respects_live_owner(repo_root: Path) -> None:
    """A lock held by a live, recent process is NOT reclaimed."""
    import json as _json
    import os as _os
    import time as _time
    from unittest.mock import patch

    from bernstein.cli.commands.worktrees_cmd import GC_LOCK_RELPATH

    lock_path = repo_root / GC_LOCK_RELPATH
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(_json.dumps({"pid": _os.getpid(), "started_at": _time.time()}), encoding="utf-8")

    with patch("bernstein.cli.commands.worktrees_cmd.is_process_alive", return_value=True):
        with pytest.raises(GcLockError):
            with lock_gc(repo_root):
                pass
