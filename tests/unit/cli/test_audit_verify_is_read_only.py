"""``bernstein audit verify`` inspects the store without writing to it (#4210).

A verifier that changes the evidence cannot be run twice on the same run: the
second pass judges what the first one wrote. These tests pin the whole command
down to bytes on disk, and pin the verdict a finished, untouched run must get
(#4201) - including the fail-closed direction.
"""

from __future__ import annotations

import hashlib
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

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


def _forbid_writes_and_the_writer_lock(monkeypatch: pytest.MonkeyPatch, root: Path) -> list[str]:
    """Make every write under *root*, and taking the chain lock, fail; return what was attempted.

    ``mkdir`` of a directory that already exists answers ``FileExistsError``,
    as a read-only filesystem does, so ``mkdir(exist_ok=True)`` on the audit
    directory is not counted as a write.
    """
    import builtins
    import io
    import os

    import bernstein.core.security.audit as audit_mod

    attempts: list[str] = []
    resolved = str(root.resolve())

    def _guard(target: object, what: str) -> None:
        if str(Path(str(target)).resolve()).startswith(resolved):
            attempts.append(f"{what} {target}")
            raise PermissionError(f"read-only: {target}")

    real_open = builtins.open
    real_os_open = os.open
    write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC

    def _open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if any(flag in mode for flag in "wax+"):
            _guard(file, f"open({mode})")
        return real_open(file, mode, *args, **kwargs)

    def _os_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if flags & write_flags:
            _guard(path, "os.open")
        return real_os_open(path, flags, *args, **kwargs)

    def _guarded(name: str, real: Callable[..., Any]) -> Callable[..., Any]:
        def _call(*args: Any, **kwargs: Any) -> Any:
            if name == "mkdir" and args and Path(str(args[0])).is_dir():
                raise FileExistsError(args[0])
            for arg in args:
                _guard(arg, f"os.{name}")
            return real(*args, **kwargs)

        return _call

    def _no_lock(*args: Any, **kwargs: Any) -> Any:
        attempts.append("chain append lock")
        raise AssertionError("verification must not take the writer lock")

    monkeypatch.setattr(builtins, "open", _open)
    monkeypatch.setattr(io, "open", _open)
    monkeypatch.setattr(os, "open", _os_open)
    for name in ("replace", "rename", "remove", "unlink", "rmdir", "mkdir"):
        monkeypatch.setattr(os, name, _guarded(name, getattr(os, name)))
    monkeypatch.setattr(audit_mod, "_chain_append_lock", _no_lock)
    return attempts


def test_verify_needs_no_write_access_and_no_writer_lock(project: Path) -> None:
    """The published layout (segments, tiles, checkpoint) verifies with writes and the lock forbidden (#3160)."""
    assert _run("seal").exit_code == 0
    _close_the_run(project)
    assert (project / ".sdd" / "audit" / "checkpoints" / "latest.json").is_file()
    assert any((project / ".sdd" / "audit" / "tiles").glob("*.tile"))

    with pytest.MonkeyPatch.context() as patch:
        attempts = _forbid_writes_and_the_writer_lock(patch, project / ".sdd")
        clean = _run("verify")

    assert attempts == []
    assert clean.exit_code == 0, clean.output

    segment = next((project / ".sdd" / "audit").glob("*.jsonl"))
    data = bytearray(segment.read_bytes())
    data[40] ^= 0x01
    segment.write_bytes(bytes(data))

    with pytest.MonkeyPatch.context() as patch:
        attempts = _forbid_writes_and_the_writer_lock(patch, project / ".sdd")
        damaged = _run("verify")

    assert attempts == []
    assert damaged.exit_code == 1
