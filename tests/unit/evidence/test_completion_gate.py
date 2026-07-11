"""Auto-seal-on-completion gate tests (issue #2362, AC1).

The orchestrator completion path seals a verification-evidence bundle for a
task that declares evidence producers, right before its worktree is reclaimed.
Sealing is a strict no-op when no producers are declared, and it must never
raise: a producer or gate failure is caught and swallowed so a task completion
can never be blocked, delayed-fatally, or failed by evidence sealing.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from bernstein.core.evidence import completion_gate
from bernstein.core.evidence.bundle import (
    read_evidence_bundle,
    verify_evidence_bundle,
)
from bernstein.core.evidence.completion_gate import seal_evidence_on_completion
from bernstein.core.tasks.models import Task

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _task(task_id: str, producers: list[dict[str, object]]) -> Task:
    """Build a minimal task carrying the given evidence producers."""
    return Task(
        id=task_id,
        title="do the thing",
        description="body",
        role="backend",
        evidence_producers=producers,
    )


def _isolate_audit_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the install audit key under tmp so the gate is fully cwd-independent."""
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))


def test_declared_producers_seal_a_verifiable_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A completing task with a declared producer gets a sealed, verifiable bundle."""
    _isolate_audit_key(tmp_path, monkeypatch)
    task = _task(
        "T-seal-1",
        [{"name": "tests", "kind": "test", "command": [sys.executable, "-c", "print('ok')"], "required": True}],
    )

    bundle = seal_evidence_on_completion(tmp_path, task, timestamp=1234)

    assert bundle is not None
    assert bundle.task_id == "T-seal-1"
    # The bundle is persisted and recomputes offline from the stored evidence.
    reloaded = read_evidence_bundle(tmp_path, "T-seal-1")
    assert reloaded is not None
    result = verify_evidence_bundle(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=(tmp_path / "audit.key").read_bytes(),
        task_id="T-seal-1",
    )
    assert result.ok, result.reason


def test_no_producers_is_a_zero_touch_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A task that declares no producers is untouched: no gate, no evidence dir."""
    _isolate_audit_key(tmp_path, monkeypatch)
    task = _task("T-empty", [])

    bundle = seal_evidence_on_completion(tmp_path, task, timestamp=1234)

    assert bundle is None
    # Zero overhead: nothing was written under the workdir.
    assert not (tmp_path / ".sdd" / "evidence").exists()


def test_gate_exception_is_swallowed_and_never_fails_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When run_evidence_gate raises, the seal returns None instead of propagating."""
    _isolate_audit_key(tmp_path, monkeypatch)

    def _boom(**_kwargs: object) -> object:
        raise RuntimeError("producer exploded")

    monkeypatch.setattr(completion_gate, "run_evidence_gate", _boom)
    task = _task(
        "T-boom",
        [{"name": "tests", "kind": "test", "command": ["true"], "required": True}],
    )

    # Must NOT raise; the completion path relies on this guard.
    bundle = seal_evidence_on_completion(tmp_path, task, timestamp=1234)

    assert bundle is None


def test_malformed_producer_spec_is_swallowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A bad producer spec (parse error) is caught: the seal never raises."""
    _isolate_audit_key(tmp_path, monkeypatch)
    task = _task(
        "T-bad",
        [{"name": "x", "kind": "not-a-real-kind", "command": ["true"], "required": True}],
    )

    bundle = seal_evidence_on_completion(tmp_path, task, timestamp=1234)

    assert bundle is None
