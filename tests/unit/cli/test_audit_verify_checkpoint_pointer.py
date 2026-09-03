"""``bernstein audit verify`` consults the newest-checkpoint pointer.

The checkpoints ledger is append-only, so truncating it back over a pin used
to be invisible to the checkpoint pillar: the shorter file still validates,
and the older pin it now ends on is still consistently extended by the
history. The atomically-replaced pointer names the checkpoint that was
actually published, so the verifier can tell the difference.
"""

from __future__ import annotations

import stat
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner, Result

from bernstein.cli.commands.audit_cmd import audit_group
from bernstein.core.persistence.chain_checkpoint import checkpoints_path, latest_pointer_path
from bernstein.core.security.audit import AUDIT_KEY_ENV, AuditLog, load_or_create_audit_key

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated project with its own chain and a pinned tmp HMAC key."""
    key_path = tmp_path / "audit.key"
    monkeypatch.setenv(AUDIT_KEY_ENV, str(key_path))
    monkeypatch.chdir(tmp_path)
    key = load_or_create_audit_key()
    key_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    log = AuditLog(tmp_path / ".sdd" / "audit", key=key)
    for i in range(3):
        log.log("task.complete", "agent-1", "task", f"t-{i}", {"i": i})
    return tmp_path


def _run(*args: str) -> Result:
    return CliRunner().invoke(audit_group, list(args))


def _audit_dir(project: Path) -> Path:
    return project / ".sdd" / "audit"


def _seal(project: Path) -> Result:
    return _run("seal")


def _append(project: Path, marker: str) -> None:
    key = load_or_create_audit_key()
    AuditLog(_audit_dir(project), key=key).log("task.complete", "agent-1", "task", marker, {})


def test_seal_publishes_the_pointer_for_the_verifier(project: Path) -> None:
    """13. The command that records a pin also publishes the object readers open."""
    assert _seal(project).exit_code == 0
    assert latest_pointer_path(_audit_dir(project)).is_file()


def test_verify_reports_a_checkpoint_ledger_truncated_over_a_published_pin(project: Path) -> None:
    """14. Load-bearing: the verdict names the pointer file, and verify fails."""
    assert _seal(project).exit_code == 0
    _append(project, "later")
    assert _seal(project).exit_code == 0

    ledger = checkpoints_path(_audit_dir(project))
    ledger.write_bytes(ledger.read_bytes().split(b"\n")[0] + b"\n")

    result = _run("verify")
    assert result.exit_code != 0
    assert "latest.json" in result.output


def test_verify_passes_on_an_untouched_checkpoint_layout(project: Path) -> None:
    """15. Positive control: the pointer check adds no verdict of its own."""
    assert _seal(project).exit_code == 0
    _append(project, "later")
    assert _seal(project).exit_code == 0

    result = _run("verify")
    assert result.exit_code == 0
    assert "latest.json" not in result.output
