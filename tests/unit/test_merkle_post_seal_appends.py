"""The daily seal pins a prefix, not the whole file (issue #4201).

The audit log is append-only and the seal is pinned at run finalization, so a
segment that keeps growing legitimately is longer than the seal recorded.
Binding the whole current file made every re-verification of a finished,
untouched run report ``TAMPERED``. These tests assert the two halves of the
replacement rule empirically: rows past the pin are counted, and any edit
inside the pinned prefix (including a shrink) still fails closed.
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING

from bernstein.core.persistence.merkle import compute_seal, save_seal, verify_merkle
from bernstein.core.security.audit import AuditLog

if TYPE_CHECKING:
    from pathlib import Path


def _log(audit_dir: Path, key: bytes, count: int) -> AuditLog:
    """Append *count* genuinely HMAC-chained events and return the log."""
    log = AuditLog(audit_dir, key=key)
    for i in range(count):
        log.log(
            event_type="task.complete",
            actor="agent-1",
            resource_type="task",
            resource_id=f"t-{i}",
            details={"i": i},
        )
    return log


def _sealed_project(tmp_path: Path) -> tuple[Path, Path, Path, bytes]:
    """Seal a 3-event chain; return ``(audit, merkle, segment, key)``."""
    audit = tmp_path / "audit"
    merkle = audit / "merkle"
    key = secrets.token_bytes(32)
    _log(audit, key, 3)
    segment = sorted(audit.glob("*.jsonl"))[0]

    _, seal = compute_seal(audit, key=key, checkpoint_gate=False)
    save_seal(seal, merkle)
    assert verify_merkle(audit, merkle).valid, "fixture must seal clean"
    return audit, merkle, segment, key


def test_rows_appended_after_the_seal_are_not_tamper(tmp_path: Path) -> None:
    """A finished run keeps appending; the sealed prefix is still intact."""
    audit, merkle, _segment, key = _sealed_project(tmp_path)

    AuditLog(audit, key=key).log(
        event_type="run.closure",
        actor="orchestrator",
        resource_type="run",
        resource_id="run-1",
        details={},
    )

    result = verify_merkle(audit, merkle)
    assert result.valid, result.errors
    assert sum(result.post_seal_rows.values()) == 1


def test_repeated_verification_of_an_untouched_run_stays_green(tmp_path: Path) -> None:
    """Verification is a pure read: the verdict does not drift across runs."""
    audit, merkle, _segment, _key = _sealed_project(tmp_path)

    first = verify_merkle(audit, merkle)
    second = verify_merkle(audit, merkle)
    assert first.valid and second.valid
    assert first.post_seal_rows == second.post_seal_rows


def test_edit_inside_the_sealed_prefix_still_fails_closed(tmp_path: Path) -> None:
    """Post-seal growth must not become cover for rewriting sealed history."""
    audit, merkle, segment, key = _sealed_project(tmp_path)

    AuditLog(audit, key=key).log(
        event_type="run.closure",
        actor="orchestrator",
        resource_type="run",
        resource_id="run-1",
        details={},
    )
    content = bytearray(segment.read_bytes())
    content[5] ^= 0x01  # a byte well inside the first sealed record
    segment.write_bytes(bytes(content))

    result = verify_merkle(audit, merkle)
    assert not result.valid
    assert any("TAMPERED" in err and segment.name in err for err in result.errors)


def test_a_segment_shorter_than_its_seal_fails_closed(tmp_path: Path) -> None:
    """A truncation removes sealed bytes; a prefix rule must not excuse it."""
    audit, merkle, segment, _key = _sealed_project(tmp_path)

    segment.write_bytes(segment.read_bytes()[:-40])

    result = verify_merkle(audit, merkle)
    assert not result.valid
    assert any("TAMPERED" in err and segment.name in err for err in result.errors)
