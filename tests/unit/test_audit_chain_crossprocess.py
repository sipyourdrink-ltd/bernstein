"""Cross-process audit-chain integrity (issue #2791).

Two independent defects broke ``bernstein audit verify`` on a log written
entirely by the product on one machine:

1. The task server / dashboard signed audit-chain entries with a different
   HMAC key than the verifier and spawner (workspace key vs XDG key).
2. Appends to the shared daily log had no cross-process lock, so two
   processes recovered the same chain tail and wrote sibling records.

These tests pin both fixes: the two key resolvers agree when no key-path
override is set, and N concurrent processes appending to one daily log
produce a chain that ``verify()`` accepts.
"""

from __future__ import annotations

import multiprocessing as mp
from pathlib import Path

import pytest

from bernstein.core.security.audit import (
    AuditLog,
    load_or_create_audit_key,
)
from bernstein.core.server.dashboard_tokens import resolve_dashboard_hmac_key

_KEY = b"cross-process-test-key-0123456789"


@pytest.mark.audit_key_real
def test_key_resolvers_agree_without_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """resolve_dashboard_hmac_key and load_or_create_audit_key agree (#2791).

    With no ``BERNSTEIN_AUDIT_KEY_PATH`` override, the dashboard/task-server
    resolver and the verifier/spawner resolver must return the same key from
    the same file. Before the fix they diverged: the dashboard resolver used
    the workspace ``.sdd/keys/audit.key`` while the verifier used the XDG
    state key.
    """
    monkeypatch.delenv("BERNSTEIN_AUDIT_KEY_PATH", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))

    sdd_dir = tmp_path / ".sdd"
    sdd_dir.mkdir(parents=True)

    dashboard_key = resolve_dashboard_hmac_key(sdd_dir)
    verifier_key = load_or_create_audit_key()

    assert dashboard_key == verifier_key
    # The dashboard resolver must not mint a separate workspace key file.
    assert not (sdd_dir / "keys" / "audit.key").exists()


def _append_worker(audit_dir_str: str, key: bytes, count: int, barrier: object) -> None:
    """Append ``count`` events to the shared daily log from a child process."""
    from bernstein.core.security.audit import AuditLog

    log = AuditLog(Path(audit_dir_str), key=key)
    barrier.wait()  # type: ignore[attr-defined]
    for i in range(count):
        log.log("concurrent.event", "worker", "res", f"id-{i}")


def test_concurrent_process_appends_verify(tmp_path: Path) -> None:
    """N processes appending M events each yield a chain verify() accepts (#2791).

    Without a cross-process lock, every worker recovers the same tail at
    construction and appends a sibling record, forking the HMAC chain.
    """
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()

    n_procs = 4
    m_events = 20

    ctx = mp.get_context("fork")
    barrier = ctx.Barrier(n_procs)
    procs = [
        ctx.Process(target=_append_worker, args=(str(audit_dir), _KEY, m_events, barrier))
        for _ in range(n_procs)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)
        assert p.exitcode == 0

    valid, errors = AuditLog(audit_dir, key=_KEY).verify()
    assert valid, f"chain forked under concurrent appends: {errors[:5]}"

    total_lines = sum(
        len([ln for ln in path.read_bytes().split(b"\n") if ln])
        for path in audit_dir.glob("*.jsonl")
    )
    assert total_lines == n_procs * m_events
