"""``bernstein audit verify`` inspects the store without writing to it (#4210).

A verifier that changes the evidence cannot be run twice on the same run: the
second pass judges what the first one wrote. These tests pin the whole command
down to bytes on disk, and pin the verdict a finished, untouched run must get
(#4201) - including the fail-closed direction.
"""

from __future__ import annotations

import hashlib
import stat
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.audit_cmd import audit_group
from bernstein.core.security.audit import AUDIT_KEY_ENV, AuditLog, load_or_create_audit_key


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated project with its own chain and a pinned tmp HMAC key.

    ``AUDIT_DIR`` in the command module is the relative ``Path(".sdd/audit")``,
    so chdir-ing here keeps every pillar inside *tmp_path*.
    """
    key_path = tmp_path / "audit.key"
    monkeypatch.setenv(AUDIT_KEY_ENV, str(key_path))
    monkeypatch.chdir(tmp_path)
    key = load_or_create_audit_key()
    key_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    log = AuditLog(tmp_path / ".sdd" / "audit", key=key)
    for i in range(3):
        log.log("task.complete", "agent-1", "task", f"t-{i}", {"i": i})
    return tmp_path


def _digest(root: Path) -> dict[str, str]:
    """Content digest of every file in *root*, keyed by relative path."""
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _close_the_run(project: Path) -> None:
    """Append the row a run legitimately writes after its seal is pinned."""
    key = load_or_create_audit_key()
    AuditLog(project / ".sdd" / "audit", key=key).log("run.closure", "orchestrator", "run", "run-1", {})


def _run(*args: str):
    return CliRunner().invoke(audit_group, list(args))


def test_two_verify_runs_leave_the_store_byte_identical(project: Path) -> None:
    """Nothing about running the verifier changes what the verifier reads."""
    assert _run("seal").exit_code == 0
    _close_the_run(project)

    before = _digest(project)
    _run("verify")
    after_first = _digest(project)
    _run("verify")
    after_second = _digest(project)

    assert after_first == before
    assert after_second == before


def test_verify_mints_no_audit_key(project: Path) -> None:
    """A verifier that minted a key would authenticate nothing and alarm."""
    assert _run("seal").exit_code == 0
    (project / "audit.key").unlink()

    _run("verify")

    assert not (project / "audit.key").exists()


def test_finished_untouched_run_verifies_clean_and_names_post_seal_rows(project: Path) -> None:
    """The seal is pinned at finalization; the rows after it are not damage."""
    assert _run("seal").exit_code == 0
    _close_the_run(project)

    result = _run("verify")

    assert result.exit_code == 0, result.output
    assert "Sealed prefix" in result.output
    assert "intact" in result.output
    assert "Post-seal rows" in result.output


def test_edit_inside_the_sealed_prefix_exits_nonzero(project: Path) -> None:
    """Post-seal growth must not become cover for rewriting sealed history."""
    assert _run("seal").exit_code == 0
    _close_the_run(project)
    segment = sorted((project / ".sdd" / "audit").glob("*.jsonl"))[0]
    content = bytearray(segment.read_bytes())
    content[5] ^= 0x01
    segment.write_bytes(bytes(content))

    result = _run("verify")

    assert result.exit_code != 0
    assert "TAMPERED" in result.output
