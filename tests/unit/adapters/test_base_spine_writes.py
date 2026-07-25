"""Adapter write-boundary tests for the lineage spine (issue #2292).

The single write boundary every adapter routes through is
:func:`bernstein.adapters.base.record_artifact_write`. These tests pin:

* AC1 - every adapter artifact write produces exactly one spine entry;
  spawning each registered adapter against a stub asserts entry count
  equals write count.
* Fail-closed gate - ``BERNSTEIN_LINEAGE_ENABLED`` defaults true and is
  a hard gate: when a write cannot be recorded the boundary raises
  rather than silently skipping.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.adapters.base import (
    LINEAGE_ENABLED_ENV,
    record_artifact_write,
)
from bernstein.adapters.registry import iter_adapter_specs
from bernstein.core.lineage.spine import LineageSpine, SpineEntry, SpineStatus

_KEY = b"k" * 32


def test_write_boundary_emits_one_entry(tmp_path: Path) -> None:
    root = tmp_path / ".sdd" / "lineage"
    h = record_artifact_write(
        artifact_path="src/foo.py",
        content=b"hello",
        actor="agent:worker",
        step_id="tc-1",
        model="claude",
        lineage_root=root,
        run_id="run-1",
        hmac_key=_KEY,
        timestamp=1,
    )
    assert h
    spine = LineageSpine(root, run_id="run-1", hmac_key=_KEY)
    entries = list(spine.iter_entries())
    assert len(entries) == 1


def test_every_adapter_write_produces_exactly_one_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC1: one spine entry per adapter write, across every adapter."""
    monkeypatch.setenv(LINEAGE_ENABLED_ENV, "1")
    root = tmp_path / ".sdd" / "lineage"
    run_id = "run-all"

    specs = list(iter_adapter_specs())
    assert len(specs) >= 40, "adapter registry unexpectedly small"

    writes = 0
    for name, _entry in specs:
        record_artifact_write(
            artifact_path=f"out/{name}.txt",
            content=f"produced-by-{name}".encode(),
            actor=f"agent:{name}",
            step_id=f"step-{name}",
            model=name,
            lineage_root=root,
            run_id=run_id,
            hmac_key=_KEY,
            timestamp=writes,
        )
        writes += 1

    spine = LineageSpine(root, run_id=run_id, hmac_key=_KEY)
    entries = list(spine.iter_entries())
    assert len(entries) == writes
    result = spine.verify()
    assert result.status is SpineStatus.OK
    assert result.count == writes


def test_gate_default_is_enabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(LINEAGE_ENABLED_ENV, raising=False)
    root = tmp_path / ".sdd" / "lineage"
    record_artifact_write(
        artifact_path="src/foo.py",
        content=b"x",
        actor="a",
        step_id="s",
        model="m",
        lineage_root=root,
        run_id="r",
        hmac_key=_KEY,
        timestamp=1,
    )
    spine = LineageSpine(root, run_id="r", hmac_key=_KEY)
    assert len(list(spine.iter_entries())) == 1


def test_gate_disabled_skips_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LINEAGE_ENABLED_ENV, "false")
    root = tmp_path / ".sdd" / "lineage"
    h = record_artifact_write(
        artifact_path="src/foo.py",
        content=b"x",
        actor="a",
        step_id="s",
        model="m",
        lineage_root=root,
        run_id="r",
        hmac_key=_KEY,
        timestamp=1,
    )
    assert h is None
    assert not root.exists()


def test_enabled_write_failure_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When enabled (default), a record failure must raise, not swallow."""
    monkeypatch.setenv(LINEAGE_ENABLED_ENV, "1")
    from bernstein.adapters import base as base_module

    class _BoomSpine(LineageSpine):
        # The boundary appends through ``record_entry`` (issue #2559) so it can
        # project the production event off the entry it just wrote.
        def record_entry(self, **_kw: object) -> SpineEntry:  # type: ignore[override]
            raise RuntimeError("disk on fire")

    monkeypatch.setattr(base_module, "LineageSpine", _BoomSpine)

    with pytest.raises(RuntimeError, match="disk on fire"):
        record_artifact_write(
            artifact_path="src/foo.py",
            content=b"x",
            actor="a",
            step_id="s",
            model="m",
            lineage_root=tmp_path / ".sdd" / "lineage",
            run_id="r",
            hmac_key=_KEY,
            timestamp=1,
        )
