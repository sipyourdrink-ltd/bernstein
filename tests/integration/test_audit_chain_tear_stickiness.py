"""A shrunk or damaged audit history cannot obtain a fresh accepted seal.

The failure this pins was reproduced through real CLI invocations: with a
seal in place and ``verify`` green, a crash truncated the newest segment
back to a record boundary. The HMAC chain stayed intact, so the next
scheduled ``bernstein audit seal`` recomputed a root over the shrunk history
and ``verify`` went green again - records were gone and nobody was paged.

With signed checkpoints the flip is dead:

* the seal job exits non-zero naming the checkpoint it conflicts with,
* ``verify`` reports the divergence as tear evidence,
* rerunning the seal job changes nothing (self-clear is gone),
* only an explicit ``bernstein audit ack-tear`` lets the next seal record a
  new pin - and the superseded checkpoint stays on disk permanently.

Everything here drives the real CLI in a subprocess, matching how the cron
jobs invoke it.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from bernstein.core.security.audit import AuditLog, load_or_create_audit_key

pytestmark = pytest.mark.integration


def _cli(workdir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("BERNSTEIN_AUTOMATION_BRIDGE_ROOT", None)
    return subprocess.run(
        [sys.executable, "-m", "bernstein", *args],
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


@pytest.fixture()
def workdir(tmp_path: Path) -> Path:
    """An isolated project whose chain holds eight sealed records."""
    (tmp_path / ".sdd" / "audit").mkdir(parents=True)
    log = AuditLog(tmp_path / ".sdd" / "audit")
    for i in range(8):
        log.log("test.event", "tester", "task", f"t-{i}", {"i": i})
    return tmp_path


def _segment(workdir: Path) -> Path:
    return sorted((workdir / ".sdd" / "audit").glob("*.jsonl"))[0]


def _truncate_to_records(workdir: Path, count: int) -> None:
    """Cut the newest segment back to *count* complete records (clean boundary)."""
    segment = _segment(workdir)
    lines = [line for line in segment.read_bytes().split(b"\n") if line]
    assert len(lines) > count
    segment.write_bytes(b"\n".join(lines[:count]) + b"\n")


def _seal_and_verify_green(workdir: Path) -> None:
    sealed = _cli(workdir, "audit", "seal")
    assert sealed.returncode == 0, sealed.stdout + sealed.stderr
    verified = _cli(workdir, "audit", "verify")
    assert verified.returncode == 0, verified.stdout + verified.stderr


class TestTruncationIsSticky:
    def test_boundary_truncation_cannot_obtain_a_fresh_seal(self, workdir: Path) -> None:
        """The exact laundering reproduction, flipped.

        A record-boundary truncation leaves the HMAC chain intact, which is
        why the old precheck (chain verification only) waved it through.
        """
        _seal_and_verify_green(workdir)

        _truncate_to_records(workdir, 4)

        sealed = _cli(workdir, "audit", "seal")
        assert sealed.returncode != 0, "the seal job must refuse a shrunk history"
        assert "Checkpoint Divergence" in sealed.stdout
        assert "checkpoint root" in sealed.stdout, "the refusal must name the checkpoint it conflicts with"
        assert "pinned 8 entries" in sealed.stdout

        verified = _cli(workdir, "audit", "verify")
        assert verified.returncode != 0
        assert "tear evidence" in verified.stdout

        # Self-clear is dead: rerunning the seal job changes nothing.
        again = _cli(workdir, "audit", "seal")
        assert again.returncode != 0
        assert "Checkpoint Divergence" in again.stdout
        still = _cli(workdir, "audit", "verify")
        assert still.returncode != 0
        assert "tear evidence" in still.stdout

    def test_midrecord_truncation_cannot_obtain_a_fresh_seal(self, workdir: Path) -> None:
        _seal_and_verify_green(workdir)

        segment = _segment(workdir)
        raw = segment.read_bytes()
        segment.write_bytes(raw[: len(raw) - 40])  # tears the final record mid-line

        sealed = _cli(workdir, "audit", "seal")
        assert sealed.returncode != 0
        assert "Checkpoint Divergence" in sealed.stdout

        verified = _cli(workdir, "audit", "verify")
        assert verified.returncode != 0
        assert "tear evidence" in verified.stdout

    def test_garbage_tail_is_tear_evidence_and_survives_sealing(self, workdir: Path) -> None:
        """The crash model is arbitrary bytes, not a clean prefix.

        A garbage tail does not shrink the checkpointed history, so the
        refusal comes from the tear pillar; the evidence survives the append
        that seals it and every rerun of the seal job.
        """
        _seal_and_verify_green(workdir)

        segment = _segment(workdir)
        with segment.open("ab") as fh:
            fh.write(b"\x80\xffnot a record{")

        sealed = _cli(workdir, "audit", "seal")
        assert sealed.returncode != 0
        assert "unacknowledged tear evidence" in sealed.stdout

        verified = _cli(workdir, "audit", "verify")
        assert verified.returncode != 0
        assert "Tear Evidence" in verified.stdout
        assert "garbage_bytes" in verified.stdout

        # An ordinary append seals the tear in-chain; the evidence stays.
        AuditLog(workdir / ".sdd" / "audit").log("test.event", "tester", "task", "after-crash", {})
        after_append = _cli(workdir, "audit", "verify")
        assert after_append.returncode != 0
        assert "garbage_bytes" in after_append.stdout
        resealed = _cli(workdir, "audit", "seal")
        assert resealed.returncode != 0, "sealing must not clear tear evidence"

    def test_acknowledgement_is_the_only_way_forward(self, workdir: Path) -> None:
        _seal_and_verify_green(workdir)
        _truncate_to_records(workdir, 4)

        offset = _segment(workdir).stat().st_size
        segment_name = _segment(workdir).name

        wrong = _cli(
            workdir,
            "audit",
            "ack-tear",
            "--segment",
            segment_name,
            "--offset",
            str(offset + 1),
            "--reason",
            "wrong offset",
        )
        assert wrong.returncode != 0, "an acknowledgement must name the tear exactly as verify printed it"

        acked = _cli(
            workdir,
            "audit",
            "ack-tear",
            "--segment",
            segment_name,
            "--offset",
            str(offset),
            "--reason",
            "host crash investigated; lost records restored from the task store",
        )
        assert acked.returncode == 0, acked.stdout + acked.stderr

        sealed = _cli(workdir, "audit", "seal")
        assert sealed.returncode == 0, sealed.stdout + sealed.stderr
        assert "acknowledged divergence" in sealed.stdout

        verified = _cli(workdir, "audit", "verify")
        assert verified.returncode == 0, verified.stdout + verified.stderr

        # The superseded checkpoint is still on disk: evidence is permanent.
        checkpoints = workdir / ".sdd" / "audit" / "checkpoints" / "checkpoints.jsonl"
        payloads = [json.loads(line)["payload"] for line in checkpoints.read_text().splitlines()]
        assert len(payloads) == 2
        assert payloads[0]["entry_count"] == 8
        assert payloads[1]["extends_prev"] is False
        assert payloads[1]["divergence_ack"]["checkpoint_root"] == payloads[0]["root_hash"]

    def test_normal_growth_seals_cleanly_and_advances_the_checkpoint(self, workdir: Path) -> None:
        """The positive path: append-only growth is a consistent extension."""
        _seal_and_verify_green(workdir)

        log = AuditLog(workdir / ".sdd" / "audit")
        for i in range(3):
            log.log("test.event", "tester", "task", f"grown-{i}", {})

        sealed = _cli(workdir, "audit", "seal")
        assert sealed.returncode == 0, sealed.stdout + sealed.stderr
        assert "extends previous" in sealed.stdout

        verified = _cli(workdir, "audit", "verify")
        assert verified.returncode == 0, verified.stdout + verified.stderr

        checkpoints = workdir / ".sdd" / "audit" / "checkpoints" / "checkpoints.jsonl"
        payloads = [json.loads(line)["payload"] for line in checkpoints.read_text().splitlines()]
        assert [p["entry_count"] for p in payloads] == [8, 11]
        assert payloads[1]["extends_prev"] is True


class TestDeterminism:
    def test_identical_directories_produce_byte_identical_checkpoints(self, tmp_path: Path) -> None:
        """Checkpoints are a pure function of directory content and key."""
        first = tmp_path / "first"
        (first / ".sdd" / "audit").mkdir(parents=True)
        log = AuditLog(first / ".sdd" / "audit")
        for i in range(6):
            log.log("test.event", "tester", "task", f"t-{i}", {"i": i})

        second = tmp_path / "second"
        (second / ".sdd").mkdir(parents=True)
        import shutil

        shutil.copytree(first / ".sdd" / "audit", second / ".sdd" / "audit")

        for workdir in (first, second):
            sealed = _cli(workdir, "audit", "seal")
            assert sealed.returncode == 0, sealed.stdout + sealed.stderr

        first_bytes = (first / ".sdd" / "audit" / "checkpoints" / "checkpoints.jsonl").read_bytes()
        second_bytes = (second / ".sdd" / "audit" / "checkpoints" / "checkpoints.jsonl").read_bytes()
        assert first_bytes == second_bytes

    def test_resealing_an_unchanged_log_is_idempotent(self, workdir: Path) -> None:
        _seal_and_verify_green(workdir)
        checkpoints = workdir / ".sdd" / "audit" / "checkpoints" / "checkpoints.jsonl"
        before = checkpoints.read_bytes()

        again = _cli(workdir, "audit", "seal")
        assert again.returncode == 0

        assert checkpoints.read_bytes() == before


class TestReadOnlyVerify:
    def test_verify_works_against_a_read_only_audit_dir(self, workdir: Path) -> None:
        """Verification is a pure read: an offline copy must verify as-is."""
        _seal_and_verify_green(workdir)

        audit_dir = workdir / ".sdd" / "audit"
        stripped: list[tuple[Path, int]] = []
        for path in [audit_dir, *audit_dir.rglob("*")]:
            mode = stat.S_IMODE(path.stat().st_mode)
            stripped.append((path, mode))
            path.chmod(mode & ~0o222)
        try:
            verified = _cli(workdir, "audit", "verify")
            assert verified.returncode == 0, verified.stdout + verified.stderr
        finally:
            for path, mode in reversed(stripped):
                path.chmod(mode)


@pytest.mark.slow
class TestConcurrencySanity:
    def test_paced_multiprocess_appends_lose_nothing(self, tmp_path: Path) -> None:
        """N processes, M appends each: zero lost, zero refused, chain verifies.

        The chain lock is a blocking ``flock``: a writer waits, it is never
        refused. The tear-seal probe sits behind the (path, size) fast path,
        so contention exercises the slow path on every append here.
        """
        import multiprocessing as mp
        import time

        audit_dir = tmp_path / "audit"
        audit_dir.mkdir()
        key = load_or_create_audit_key()

        n_procs = 8
        m_events = 25

        def _worker(worker_id: int, barrier: object) -> None:
            log = AuditLog(audit_dir, key=key)
            barrier.wait()  # type: ignore[attr-defined]
            for i in range(m_events):
                log.log("concurrent.event", f"worker-{worker_id}", "res", f"w{worker_id}-{i}")
                time.sleep(0.002)

        ctx = mp.get_context("fork")
        barrier = ctx.Barrier(n_procs)
        procs = [ctx.Process(target=_worker, args=(w, barrier)) for w in range(n_procs)]
        for proc in procs:
            proc.start()
        for proc in procs:
            proc.join(timeout=120)
        stuck = [proc for proc in procs if proc.is_alive()]
        for proc in stuck:
            proc.terminate()
            proc.join(timeout=5)
        assert not stuck, "worker process(es) did not exit within the timeout"
        assert all(proc.exitcode == 0 for proc in procs), "no append may be refused or crash"

        log = AuditLog(audit_dir, key=key)
        events = log.query(event_type="concurrent.event")
        assert len(events) == n_procs * m_events, "no append may be lost"
        ids = {event.resource_id for event in events}
        assert len(ids) == n_procs * m_events, "every append is distinct"

        ok, errors = log.verify()
        assert ok, errors
