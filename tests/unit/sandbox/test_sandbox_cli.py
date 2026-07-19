"""CLI-surface tests for ``bernstein sandbox fork-race`` base validation (#2613).

These pin the guard added in response to review: a malformed *or* absent
``--base`` must fail as a clean ``ClickException`` (exit 1, no traceback) and,
critically, *before* any side-effectful state - no Ed25519 signing key is
minted for a doomed run.
"""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.sandbox_cmd import sandbox_group

if TYPE_CHECKING:
    from pathlib import Path


def _invoke(base: str, tmp_path: Path) -> tuple[int, str, bool]:
    key_path = tmp_path / "keys" / "selection.key"
    result = CliRunner().invoke(
        sandbox_group,
        [
            "fork-race",
            "--base",
            base,
            "--cmd",
            "true",
            "--out",
            str(tmp_path / "receipt.json"),
            "--cas-dir",
            str(tmp_path / "cas"),
            "--key",
            str(key_path),
            "--audit-dir",
            str(tmp_path / "audit"),
        ],
    )
    return result.exit_code, (result.output or ""), key_path.exists()


def test_fork_race_malformed_base_fails_cleanly_without_minting_key(tmp_path: Path) -> None:
    exit_code, output, key_minted = _invoke("not-a-hex-digest", tmp_path)
    assert exit_code == 1
    assert "invalid base snapshot digest" in output
    assert not key_minted


def test_fork_race_absent_base_fails_cleanly_without_minting_key(tmp_path: Path) -> None:
    # Well-formed 64-char hex digest that is simply not present in the CAS.
    exit_code, output, key_minted = _invoke("0" * 64, tmp_path)
    assert exit_code == 1
    assert "not found in CAS" in output
    assert not key_minted


def _seed_present_base(cas_dir: Path) -> str:
    """Materialise a real base snapshot in ``cas_dir`` and return its digest.

    Uses the fake microVM monitor so the CAS ends up with a genuinely
    *present* base. That lets the ``--out`` preflight tests below use a valid
    ``--base``: without the preflight, a present base makes the command proceed
    to mint a signing key and create the audit dir *before* it ever tries to
    write the receipt, so those side effects are the discriminator.
    """
    import asyncio

    from bernstein.core.persistence.cas_store import CASStore
    from bernstein.core.sandbox.backends._vmmonitor import FakeMonitor
    from bernstein.core.sandbox.backends.microvm import MicroVMSandboxBackend
    from bernstein.core.sandbox.manifest import FileEntry, WorkspaceManifest

    async def _seed() -> str:
        cas = CASStore(cas_dir)
        backend = MicroVMSandboxBackend(monitor_factory=lambda root: FakeMonitor(root=root), cas=cas)
        session = await backend.create(
            WorkspaceManifest(root="/workspace", files=(FileEntry(path="b.txt", content=b"BASE"),)),
        )
        base = await session.snapshot()
        await backend.destroy(session)
        return base

    return asyncio.run(_seed())


def _invoke_fork_race_out(base: str, out_path: Path, tmp_path: Path) -> tuple[int, str, bool, bool]:
    cas_dir = tmp_path / "cas"
    key_path = tmp_path / "keys" / "selection.key"
    audit_dir = tmp_path / "audit"
    result = CliRunner().invoke(
        sandbox_group,
        [
            "fork-race",
            "--base",
            base,
            "--cmd",
            "true",
            "--out",
            str(out_path),
            "--cas-dir",
            str(cas_dir),
            "--key",
            str(key_path),
            "--audit-dir",
            str(audit_dir),
        ],
    )
    return result.exit_code, (result.output or ""), key_path.exists(), audit_dir.exists()


def test_fork_race_out_nonexistent_parent_fails_before_dispatch(tmp_path: Path) -> None:
    """AC4 (#2707): a ``--out`` whose parent directory does not exist must fail
    cleanly *before* any candidate is dispatched - even with a valid, present
    ``--base``. No signing key minted, no audit dir created, no receipt written,
    no raw traceback."""
    base = _seed_present_base(tmp_path / "cas")
    out_path = tmp_path / "nonexistent-dir" / "receipt.json"  # parent missing

    exit_code, output, key_minted, audit_created = _invoke_fork_race_out(base, out_path, tmp_path)

    assert exit_code == 1, output
    assert "Traceback" not in output  # diagnosable ClickException, not a crash
    assert "parent directory does not exist" in output
    assert not key_minted, "no signing key may be minted for a doomed --out"
    assert not audit_created, "no audit dir may be created for a doomed --out"
    assert not out_path.exists()


@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0,
    reason="chmod-based unwritability is a no-op on Windows and for root",
)
def test_fork_race_out_unwritable_parent_fails_before_dispatch(tmp_path: Path) -> None:
    """AC4 (#2707): a ``--out`` whose parent exists but is not writable (0o000)
    must also fail before dispatch, with no side effects."""
    base = _seed_present_base(tmp_path / "cas")
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o000)
    out_path = locked / "receipt.json"
    try:
        exit_code, output, key_minted, audit_created = _invoke_fork_race_out(base, out_path, tmp_path)
    finally:
        locked.chmod(0o755)  # let tmp cleanup remove it

    assert exit_code == 1, output
    assert "Traceback" not in output
    assert "not writable" in output.lower()
    assert not key_minted, "no signing key may be minted for a doomed --out"
    assert not audit_created, "no audit dir may be created for a doomed --out"


@pytest.mark.asyncio
async def test_receipt_verify_distinguishes_ok_tampered_absent(tmp_path: Path) -> None:
    """`receipt verify` gives three different answers: OK (0), tampered (1), and
    absent/cannot-verify (2). Absent must NOT read as tampering."""
    from bernstein.core.orchestration.best_of_n import CandidateResult
    from bernstein.core.persistence.cas_store import CASStore
    from bernstein.core.sandbox.backends._vmmonitor import FakeMonitor
    from bernstein.core.sandbox.backends.microvm import MicroVMSandboxBackend
    from bernstein.core.sandbox.fork_race import fork_race
    from bernstein.core.sandbox.manifest import FileEntry, WorkspaceManifest
    from bernstein.core.sandbox.selection_receipt import load_or_create_signing_key, write_receipt

    cas_dir = tmp_path / "cas"
    cas = CASStore(cas_dir)
    backend = MicroVMSandboxBackend(monitor_factory=lambda root: FakeMonitor(root=root), cas=cas)
    key = load_or_create_signing_key(tmp_path / "k.key")

    base_session = await backend.create(
        WorkspaceManifest(root="/workspace", files=(FileEntry(path="b.txt", content=b"BASE"),)),
    )
    base = await base_session.snapshot()
    await backend.destroy(base_session)

    async def run_candidate(session: object, index: int) -> CandidateResult:
        await session.write(f"c{index}.txt", f"work-{index}".encode())  # type: ignore[attr-defined]
        return CandidateResult(task_id=f"candidate-{index}", tests_passing=index == 0)

    receipt = await fork_race(
        backend=backend,
        base_snapshot_digest=base,
        run_candidate=run_candidate,
        k=2,
        signing_key=key,
    )
    receipt_path = tmp_path / "receipt.json"
    write_receipt(receipt_path, receipt)

    runner = CliRunner()
    args = ["receipt", "verify", str(receipt_path), "--cas-dir", str(cas_dir)]

    ok = runner.invoke(sandbox_group, [*args, "--expected-keyid", receipt.keyid])
    assert ok.exit_code == 0, ok.output
    assert "OK" in ok.output

    # Absent loser blob -> cannot-verify / INCOMPLETE (exit 2), not tampering.
    loser = receipt.loser_snapshot_digests[0]
    (cas.root / loser[:2] / loser).unlink()
    inc = runner.invoke(sandbox_group, args)
    assert inc.exit_code == 2, inc.output
    assert "cannot-verify" in inc.output.lower()
    assert "INCOMPLETE" in inc.output  # verdict is "incomplete", not a hard tamper failure
    assert "FAILED" not in inc.output
    assert "tampered:" not in inc.output.lower()  # no per-blob tampered finding line

    # Tampered base blob (present, wrong hash) -> FAILED (exit 1), takes precedence.
    blob = cas.root / base[:2] / base
    corrupt = bytearray(blob.read_bytes())
    corrupt[3] ^= 0xFF
    blob.write_bytes(bytes(corrupt))
    bad = runner.invoke(sandbox_group, args)
    assert bad.exit_code == 1, bad.output
    assert "tampered" in bad.output.lower()


# ---------------------------------------------------------------------------
# #2705: receipt verify - anchoring, unreadable != absent, no-traceback,
# tampered precedence, and one shared CLI/library definition.
# ---------------------------------------------------------------------------


async def _make_receipt(tmp_path: Path):
    """Build a valid, complete receipt with its CAS populated (mirrors the
    fixture the OK/tampered/absent test above uses)."""
    from bernstein.core.orchestration.best_of_n import CandidateResult
    from bernstein.core.persistence.cas_store import CASStore
    from bernstein.core.sandbox.backends._vmmonitor import FakeMonitor
    from bernstein.core.sandbox.backends.microvm import MicroVMSandboxBackend
    from bernstein.core.sandbox.fork_race import fork_race
    from bernstein.core.sandbox.manifest import FileEntry, WorkspaceManifest
    from bernstein.core.sandbox.selection_receipt import load_or_create_signing_key, write_receipt

    cas_dir = tmp_path / "cas"
    cas = CASStore(cas_dir)
    backend = MicroVMSandboxBackend(monitor_factory=lambda root: FakeMonitor(root=root), cas=cas)
    key = load_or_create_signing_key(tmp_path / "k.key")

    base_session = await backend.create(
        WorkspaceManifest(root="/workspace", files=(FileEntry(path="b.txt", content=b"BASE"),)),
    )
    base = await base_session.snapshot()
    await backend.destroy(base_session)

    async def run_candidate(session: object, index: int) -> CandidateResult:
        await session.write(f"c{index}.txt", f"work-{index}".encode())  # type: ignore[attr-defined]
        return CandidateResult(task_id=f"candidate-{index}", tests_passing=index == 0)

    receipt = await fork_race(
        backend=backend,
        base_snapshot_digest=base,
        run_candidate=run_candidate,
        k=2,
        signing_key=key,
    )
    receipt_path = tmp_path / "receipt.json"
    write_receipt(receipt_path, receipt)
    return receipt, receipt_path, cas_dir


@pytest.mark.asyncio
async def test_receipt_verify_unanchored_never_exits_zero(tmp_path: Path) -> None:
    """Criterion 1: with no --expected-keyid the signer is unchecked, so an
    intact receipt must NOT exit 0 (a re-sign under any key looks identical)."""
    _receipt, receipt_path, cas_dir = await _make_receipt(tmp_path)
    res = CliRunner().invoke(
        sandbox_group,
        ["receipt", "verify", str(receipt_path), "--cas-dir", str(cas_dir)],
    )
    assert res.exit_code == 3, res.output
    assert "unanchored" in res.output.lower()
    assert "OK" not in res.output


@pytest.mark.asyncio
async def test_receipt_verify_anchored_exits_zero(tmp_path: Path) -> None:
    """The same intact receipt, anchored to its actual signer, is fully verified."""
    receipt, receipt_path, cas_dir = await _make_receipt(tmp_path)
    res = CliRunner().invoke(
        sandbox_group,
        ["receipt", "verify", str(receipt_path), "--cas-dir", str(cas_dir), "--expected-keyid", receipt.keyid],
    )
    assert res.exit_code == 0, res.output
    assert "OK" in res.output


@pytest.mark.asyncio
async def test_receipt_verify_wrong_anchor_never_exits_zero(tmp_path: Path) -> None:
    """Criterion 1: a receipt whose signer is not the trusted keyid fails (a
    body re-signed under another key must never verify clean)."""
    _receipt, receipt_path, cas_dir = await _make_receipt(tmp_path)
    res = CliRunner().invoke(
        sandbox_group,
        ["receipt", "verify", str(receipt_path), "--cas-dir", str(cas_dir), "--expected-keyid", "0" * 64],
    )
    assert res.exit_code == 1, res.output
    assert "trusted signer" in res.output.lower()


@pytest.mark.parametrize("payload", ["this is not json", '{"truncated": ', "[1, 2, 3]", ""])
def test_receipt_verify_malformed_never_tracebacks(tmp_path: Path, payload: str) -> None:
    """Criterion: no input produces a traceback; malformed input returns a
    clean, diagnosable error (not a raw JSONDecodeError)."""
    bad_path = tmp_path / "bad.json"
    bad_path.write_text(payload)
    res = CliRunner().invoke(
        sandbox_group,
        ["receipt", "verify", str(bad_path), "--cas-dir", str(tmp_path / "cas")],
    )
    assert res.exit_code != 0
    assert "Could not read a selection receipt" in res.output
    # No uncaught, non-Click exception leaked through the command.
    assert res.exception is None or isinstance(res.exception, SystemExit), res.exception


@pytest.mark.asyncio
async def test_receipt_verify_tampered_takes_precedence_over_absent(tmp_path: Path) -> None:
    """Criterion: tampered outranks absent. Corrupt one blob, remove another;
    the verdict is FAILED (1), not INCOMPLETE (2)."""
    receipt, receipt_path, cas_dir = await _make_receipt(tmp_path)
    base = receipt.base_snapshot_digest
    loser = receipt.loser_snapshot_digests[0]
    # Tamper the base blob (present, wrong hash) ...
    blob = cas_dir / base[:2] / base
    corrupt = bytearray(blob.read_bytes())
    corrupt[0] ^= 0xFF
    blob.write_bytes(bytes(corrupt))
    # ... and delete a loser blob (absent).
    (cas_dir / loser[:2] / loser).unlink()
    res = CliRunner().invoke(
        sandbox_group,
        ["receipt", "verify", str(receipt_path), "--cas-dir", str(cas_dir), "--expected-keyid", receipt.keyid],
    )
    assert res.exit_code == 1, res.output
    assert "tampered" in res.output.lower()


@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0,
    reason="chmod-based unreadability is a no-op on Windows and for root",
)
@pytest.mark.asyncio
async def test_receipt_verify_unreadable_is_not_reported_as_absent(tmp_path: Path) -> None:
    """Criteria 2+3: a present-but-unreadable blob is a reader-side problem
    (exit 4, 'unreadable'), never a crash and never 'absent'."""
    receipt, receipt_path, cas_dir = await _make_receipt(tmp_path)
    base = receipt.base_snapshot_digest
    blob = cas_dir / base[:2] / base
    blob.chmod(0o000)
    try:
        res = CliRunner().invoke(
            sandbox_group,
            ["receipt", "verify", str(receipt_path), "--cas-dir", str(cas_dir), "--expected-keyid", receipt.keyid],
        )
    finally:
        blob.chmod(0o644)  # let tmp cleanup remove it
    assert res.exit_code == 4, res.output
    assert "unreadable" in res.output.lower()
    assert "absent" not in res.output.lower()
    assert res.exception is None or isinstance(res.exception, SystemExit), res.exception


@pytest.mark.asyncio
async def test_verify_receipt_full_is_the_one_shared_definition(tmp_path: Path) -> None:
    """Criterion: the CLI and library derive the verdict from one definition.
    Exercise that definition directly across every outcome + precedence."""
    from bernstein.core.sandbox.selection_receipt import (
        BLOB_ABSENT,
        BLOB_INTACT,
        BLOB_TAMPERED,
        BLOB_UNREADABLE,
        verify_receipt_full,
    )

    receipt, _receipt_path, _cas_dir = await _make_receipt(tmp_path)
    kid = receipt.keyid

    def intact(_d: str) -> str:
        return BLOB_INTACT

    # Assert BOTH the machine-readable verdict field and the exit code (criterion
    # 4 requires the verdict string be distinguishable, not just the exit code).
    verified = verify_receipt_full(receipt, expected_keyid=kid, blob_status=intact)
    assert (verified.verdict, verified.exit_code) == ("verified", 0)
    assert verify_receipt_full(receipt, expected_keyid=None, blob_status=intact).verdict == "unanchored"
    # Empty-string anchor (e.g. an unset env var) is NOT an anchor -> unanchored,
    # never verified. (Trust-anchor bypass lead from the Codex static pass.)
    assert verify_receipt_full(receipt, expected_keyid="", blob_status=intact).exit_code == 3
    absent_v = verify_receipt_full(receipt, expected_keyid=kid, blob_status=lambda _d: BLOB_ABSENT)
    assert (absent_v.verdict, absent_v.exit_code) == ("incomplete", 2)
    unread_v = verify_receipt_full(receipt, expected_keyid=kid, blob_status=lambda _d: BLOB_UNREADABLE)
    assert (unread_v.verdict, unread_v.exit_code) == ("unreadable", 4)
    # Wrong anchor fails even with every blob intact.
    assert verify_receipt_full(receipt, expected_keyid="0" * 64, blob_status=intact).exit_code == 1
    # An unknown blob_status result must NEVER be treated as intact/verified.
    bogus = verify_receipt_full(receipt, expected_keyid=kid, blob_status=lambda _d: "surprise")
    assert bogus.exit_code != 0
    assert bogus.verdict == "unreadable"

    # Tampered outranks absent regardless of digest order.
    statuses = iter([BLOB_TAMPERED, BLOB_ABSENT])

    def mixed(_d: str) -> str:
        return next(statuses, BLOB_ABSENT)

    assert verify_receipt_full(receipt, expected_keyid=kid, blob_status=mixed).exit_code == 1


def test_receipt_verify_binary_input_never_tracebacks(tmp_path: Path) -> None:
    """C2 (Gemini High): a binary / invalid-UTF-8 file must return a clean
    verdict, not a raw UnicodeDecodeError traceback (read_text raises
    UnicodeDecodeError, a ValueError, which slips past a bare `except OSError`)."""
    bad = tmp_path / "receipt.bin"
    bad.write_bytes(b"\x00\x01\x02\xff\xfe not valid utf-8 \x80\x81\x82")
    res = CliRunner().invoke(
        sandbox_group,
        ["receipt", "verify", str(bad), "--cas-dir", str(tmp_path / "cas")],
    )
    assert res.exit_code != 0
    assert "Could not read a selection receipt" in res.output
    assert res.exception is None or isinstance(res.exception, SystemExit), res.exception


@pytest.mark.asyncio
async def test_receipt_verify_empty_keyid_is_not_a_trust_anchor(tmp_path: Path) -> None:
    """Trust-anchor bypass (Codex static lead): an empty --expected-keyid (e.g.
    an unset env var) must NOT be treated as anchored. An intact receipt with an
    empty anchor is UNANCHORED (exit 3), never verified (exit 0) - otherwise a
    forged receipt carrying keyid="" would satisfy an empty anchor."""
    _receipt, receipt_path, cas_dir = await _make_receipt(tmp_path)
    res = CliRunner().invoke(
        sandbox_group,
        ["receipt", "verify", str(receipt_path), "--cas-dir", str(cas_dir), "--expected-keyid", ""],
    )
    assert res.exit_code == 3, res.output
    assert "unanchored" in res.output.lower()
    assert "OK" not in res.output


@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0,
    reason="chmod-based unreadability is a no-op on Windows and for root",
)
@pytest.mark.asyncio
async def test_receipt_verify_unreadable_shard_is_not_reported_absent(tmp_path: Path) -> None:
    """C3 (Gemini Critical): an unreadable shard directory makes cas.get() return
    None (Path.exists() suppresses the permission error), and the old
    exists()+os.access probe then falsely reported ABSENT. It must read as
    UNREADABLE (exit 4) - a reader-side problem, never a claim about the record."""
    receipt, receipt_path, cas_dir = await _make_receipt(tmp_path)
    base = receipt.base_snapshot_digest
    shard = cas_dir / base[:2]
    shard.chmod(0o000)
    try:
        res = CliRunner().invoke(
            sandbox_group,
            ["receipt", "verify", str(receipt_path), "--cas-dir", str(cas_dir), "--expected-keyid", receipt.keyid],
        )
    finally:
        shard.chmod(0o755)  # restore so the tmp fixture can clean up
    assert res.exit_code == 4, res.output
    assert "unreadable" in res.output.lower()
    assert "absent" not in res.output.lower()
    assert res.exception is None or isinstance(res.exception, SystemExit), res.exception


@pytest.mark.asyncio
async def test_receipt_verify_malformed_digest_field_never_tracebacks(tmp_path: Path) -> None:
    """Blocker (Linus): a receipt whose digest FIELD is not 64 hex chars - a
    bit-flip inside an otherwise-parseable receipt - must return a verdict, not
    crash. CASStore.get() raises ValueError on such a digest; verify_receipt_full
    filters it as malformed and forces FAILED (exit 1), never a traceback."""
    import json

    _receipt, receipt_path, cas_dir = await _make_receipt(tmp_path)
    data = json.loads(receipt_path.read_text())
    data["base_snapshot_digest"] = "not-a-valid-hex-digest"  # not 64 hex chars
    receipt_path.write_text(json.dumps(data))

    res = CliRunner().invoke(
        sandbox_group,
        ["receipt", "verify", str(receipt_path), "--cas-dir", str(cas_dir)],
    )
    assert res.exit_code == 1, res.output
    assert res.exception is None or isinstance(res.exception, SystemExit), res.exception
    assert "malformed" in res.output.lower()
    assert "OK" not in res.output


@pytest.mark.asyncio
async def test_receipt_verify_nan_constant_never_tracebacks(tmp_path: Path) -> None:
    """New (Codex HIGH): json.loads accepts NaN/Infinity by default; such a
    constant would otherwise reach allow_nan=False during verification and raise
    an uncaught ValueError. It must be rejected at the parse boundary with a
    clean verdict, never a traceback."""
    import json

    _receipt, receipt_path, cas_dir = await _make_receipt(tmp_path)
    data = json.loads(receipt_path.read_text())
    data["__nan_probe__"] = float("nan")
    receipt_path.write_text(json.dumps(data))  # emits a bare NaN token
    res = CliRunner().invoke(
        sandbox_group,
        ["receipt", "verify", str(receipt_path), "--cas-dir", str(cas_dir)],
    )
    assert res.exit_code != 0
    assert "Could not read a selection receipt" in res.output
    assert res.exception is None or isinstance(res.exception, SystemExit), res.exception


@pytest.mark.asyncio
async def test_verify_receipt_rejects_empty_required_digests(tmp_path: Path) -> None:
    """New (Codex HIGH): a receipt with empty base/winner digests references no
    CAS objects, so a signed one could verify clean while zero blobs are checked.
    verify_receipt must reject empty/malformed required digests."""
    from dataclasses import replace

    from bernstein.core.sandbox.selection_receipt import verify_receipt

    receipt, _receipt_path, _cas_dir = await _make_receipt(tmp_path)
    empty = replace(receipt, base_snapshot_digest="", winner_snapshot_digest="")
    result = verify_receipt(empty, expected_keyid=receipt.keyid)
    assert not result.ok
    joined = " ".join(result.errors).lower()
    assert "base_snapshot_digest" in joined
    assert "winner_snapshot_digest" in joined


@pytest.mark.asyncio
async def test_receipt_verify_blob_vanished_midread_is_absent(tmp_path: Path, monkeypatch) -> None:
    """New (Codex MEDIUM): a blob deleted between the store's exists() and read
    raises FileNotFoundError; that is genuinely absent (a GC race), and must not
    be misclassified as unreadable."""
    from bernstein.core.persistence.cas_store import CASStore

    receipt, receipt_path, cas_dir = await _make_receipt(tmp_path)
    base = receipt.base_snapshot_digest
    real_get = CASStore.get

    def flaky_get(self, digest, *args, **kwargs):
        if digest == base:
            raise FileNotFoundError(2, "vanished mid-read", str(cas_dir / digest[:2] / digest))
        return real_get(self, digest, *args, **kwargs)

    monkeypatch.setattr(CASStore, "get", flaky_get)
    res = CliRunner().invoke(
        sandbox_group,
        ["receipt", "verify", str(receipt_path), "--cas-dir", str(cas_dir), "--expected-keyid", receipt.keyid],
    )
    assert res.exit_code == 2, res.output  # incomplete/absent, NOT 4 (unreadable)
    assert "absent" in res.output.lower()
    assert "unreadable" not in res.output.lower()


@pytest.mark.asyncio
async def test_cli_and_library_agree_on_exit_code(tmp_path: Path) -> None:
    """Criterion 5: the CLI exit code must EQUAL verify_receipt_full's for the
    same receipt + CAS state - compared directly, so a CLI that drifted from the
    shared definition fails this test."""
    from bernstein.core.sandbox.selection_receipt import (
        BLOB_ABSENT,
        BLOB_INTACT,
        verify_receipt_full,
    )

    receipt, receipt_path, cas_dir = await _make_receipt(tmp_path)
    kid = receipt.keyid
    runner = CliRunner()
    args = ["receipt", "verify", str(receipt_path), "--cas-dir", str(cas_dir), "--expected-keyid", kid]

    cli_ok = runner.invoke(sandbox_group, args)
    lib_ok = verify_receipt_full(receipt, expected_keyid=kid, blob_status=lambda _d: BLOB_INTACT)
    assert cli_ok.exit_code == lib_ok.exit_code == 0, cli_ok.output

    loser = receipt.loser_snapshot_digests[0]
    (cas_dir / loser[:2] / loser).unlink()
    cli_absent = runner.invoke(sandbox_group, args)
    lib_absent = verify_receipt_full(
        receipt, expected_keyid=kid, blob_status=lambda d: BLOB_ABSENT if d == loser else BLOB_INTACT
    )
    assert cli_absent.exit_code == lib_absent.exit_code == 2, cli_absent.output


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX symlink semantics")
@pytest.mark.asyncio
async def test_receipt_verify_symlinked_blob_is_unreadable_not_followed(tmp_path: Path) -> None:
    """A CAS blob replaced by a symlink must NOT be dereferenced - following it
    could read an attacker-chosen path or block on a FIFO/device. cas.get opens
    with O_NOFOLLOW so the read itself refuses the symlink atomically (no TOCTOU
    window, per CodeRabbit's race finding); the verifier reports unreadable
    (exit 4), never absent (what naively following a dangling link would yield)
    or intact."""
    receipt, receipt_path, cas_dir = await _make_receipt(tmp_path)
    base = receipt.base_snapshot_digest
    blob_path = cas_dir / base[:2] / base
    blob_path.unlink()
    # A dangling symlink: if the verifier followed it, read/stat would raise
    # FileNotFoundError and misreport "absent". O_NOFOLLOW fails on the symlink
    # itself (ELOOP) regardless of target, so it is classified unreadable.
    blob_path.symlink_to(cas_dir / "does-not-exist-target")

    res = CliRunner().invoke(
        sandbox_group,
        ["receipt", "verify", str(receipt_path), "--cas-dir", str(cas_dir), "--expected-keyid", receipt.keyid],
    )
    assert res.exit_code == 4, res.output  # unreadable, NOT 2 (absent) or 0 (intact)
    assert "unreadable" in res.output.lower()
    assert "OK" not in res.output
