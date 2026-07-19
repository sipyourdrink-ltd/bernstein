"""CLI-surface tests for ``bernstein sandbox fork-race`` base validation (#2613).

These pin the guard added in response to review: a malformed *or* absent
``--base`` must fail as a clean ``ClickException`` (exit 1, no traceback) and,
critically, *before* any side-effectful state - no Ed25519 signing key is
minted for a doomed run.
"""

from __future__ import annotations

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

    ok = runner.invoke(sandbox_group, args)
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
