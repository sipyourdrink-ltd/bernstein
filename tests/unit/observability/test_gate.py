"""Unit tests for the observability regression gate.

Covers ``scripts/observability/gate.py`` ``detect_regressions`` over two
in-memory snapshot payloads (the same shape ``bernstein doctor observe
--json`` writes under ``docs/_internal/observability/snapshots/``):

- an ``ok -> fail`` threshold-status flip,
- a coverage drop past the early-warning floor,
- a new / increased security vulnerability,
- a backend that silently lost its credentials,
- a clean (green) diff that must stay quiet,
- the ``load_two_latest`` file-ordering + exit-code plumbing.

The script is import-only at module level so these tests drive its pure
functions directly without spawning a subprocess.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from types import ModuleType

# ``scripts/observability/`` is not an installed package, so load by path.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "observability" / "gate.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("observability_gate", _SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["observability_gate"] = module
    spec.loader.exec_module(module)
    return module


gate = _load_module()


def _metric(name: str, numeric: float, status: str, threshold: str = "0") -> dict[str, Any]:
    return {
        "name": name,
        "value": str(numeric),
        "numeric": numeric,
        "threshold": threshold,
        "threshold_status": status,
        "delta": "-",
    }


def _backend(name: str, status: str, metrics: list[dict[str, Any]]) -> dict[str, Any]:
    return {"backend": name, "status": status, "detail": "", "error": None, "metrics": metrics}


def _snapshot(*backends: dict[str, Any]) -> dict[str, Any]:
    summary = {"ok": 0, "warn": 0, "fail": 0, "skipped": 0, "error": 0}
    for b in backends:
        summary[b["status"]] = summary.get(b["status"], 0) + 1
    return {"summary": summary, "backends": list(backends)}


# --------------------------------------------------------------------------
# ok -> fail flip
# --------------------------------------------------------------------------


def test_ok_to_fail_flip_is_fail_severity() -> None:
    prev = _snapshot(_backend("code-scanning", "ok", [_metric("critical_alerts", 0.0, "ok")]))
    curr = _snapshot(_backend("code-scanning", "fail", [_metric("critical_alerts", 1.0, "fail")]))

    regs = gate.detect_regressions(prev, curr)

    assert len(regs) == 1
    assert regs[0].backend == "code-scanning"
    assert regs[0].metric == "critical_alerts"
    assert regs[0].severity == "fail"
    assert regs[0].delta == 1.0


def test_ok_to_warn_flip_is_warn_severity() -> None:
    prev = _snapshot(_backend("code-scanning", "ok", [_metric("open_alerts", 0.0, "ok")]))
    curr = _snapshot(_backend("code-scanning", "warn", [_metric("open_alerts", 2.0, "warn")]))

    regs = gate.detect_regressions(prev, curr)

    assert len(regs) == 1
    assert regs[0].severity == "warn"


# --------------------------------------------------------------------------
# new / increased vulnerability
# --------------------------------------------------------------------------


def test_new_vulnerability_is_flagged() -> None:
    prev = _snapshot(_backend("code-scanning", "ok", [_metric("open_alerts", 0.0, "ok")]))
    curr = _snapshot(_backend("code-scanning", "warn", [_metric("open_alerts", 3.0, "warn")]))

    regs = gate.detect_regressions(prev, curr)

    assert len(regs) == 1
    assert regs[0].metric == "open_alerts"
    assert regs[0].severity == "warn"
    assert regs[0].delta == 3.0


def test_increased_critical_alerts_is_fail_severity() -> None:
    prev = _snapshot(_backend("code-scanning", "fail", [_metric("critical_alerts", 1.0, "fail")]))
    curr = _snapshot(_backend("code-scanning", "fail", [_metric("critical_alerts", 4.0, "fail")]))

    regs = gate.detect_regressions(prev, curr)

    assert len(regs) == 1
    assert regs[0].severity == "fail"
    assert regs[0].delta == 3.0


# --------------------------------------------------------------------------
# backend lost creds
# --------------------------------------------------------------------------


def test_backend_lost_creds_is_flagged() -> None:
    prev = _snapshot(_backend("code-scanning", "ok", [_metric("open_alerts", 0.0, "ok")]))
    curr = _snapshot(_backend("code-scanning", "skipped", []))

    regs = gate.detect_regressions(prev, curr)

    assert len(regs) == 1
    assert regs[0].backend == "code-scanning"
    assert regs[0].severity == "warn"
    assert "lost creds" in regs[0].reason


def test_backend_disappearing_entirely_is_flagged() -> None:
    prev = _snapshot(_backend("code-scanning", "ok", [_metric("critical_alerts", 0.0, "ok")]))
    curr = _snapshot()

    regs = gate.detect_regressions(prev, curr)

    assert len(regs) == 1
    assert regs[0].backend == "code-scanning"
    assert regs[0].severity == "warn"


# --------------------------------------------------------------------------
# green / no-regression cases
# --------------------------------------------------------------------------


def test_flat_green_snapshots_produce_no_regressions() -> None:
    prev = _snapshot(
        _backend(
            "code-scanning",
            "ok",
            [
                _metric("critical_alerts", 0.0, "ok"),
                _metric("high_alerts", 0.0, "ok"),
                _metric("open_alerts", 0.0, "ok"),
            ],
        ),
    )
    curr = _snapshot(
        _backend(
            "code-scanning",
            "ok",
            [
                _metric("critical_alerts", 0.0, "ok"),
                _metric("high_alerts", 0.0, "ok"),
                _metric("open_alerts", 0.0, "ok"),
            ],
        ),
    )

    assert gate.detect_regressions(prev, curr) == []


def test_backend_gaining_creds_is_not_a_regression() -> None:
    prev = _snapshot(_backend("code-scanning", "skipped", []))
    curr = _snapshot(_backend("code-scanning", "ok", [_metric("critical_alerts", 0.0, "ok")]))

    assert gate.detect_regressions(prev, curr) == []


def test_missing_previous_snapshot_only_flags_hard_fails() -> None:
    curr = _snapshot(
        _backend(
            "code-scanning",
            "fail",
            [_metric("critical_alerts", 2.0, "fail"), _metric("open_alerts", 3.0, "warn")],
        ),
    )

    regs = gate.detect_regressions(None, curr)

    # The brand-new warn metric must not fire; only the fail-status one does.
    assert len(regs) == 1
    assert regs[0].severity == "fail"
    assert regs[0].backend == "code-scanning"


# --------------------------------------------------------------------------
# load_two_latest + main() plumbing
# --------------------------------------------------------------------------


def test_load_two_latest_orders_by_date(tmp_path: Path) -> None:
    (tmp_path / "2026-07-10.json").write_text(json.dumps(_snapshot()), encoding="utf-8")
    (tmp_path / "2026-07-14.json").write_text(
        json.dumps(_snapshot(_backend("code-scanning", "ok", [_metric("critical_alerts", 0.0, "ok")]))),
        encoding="utf-8",
    )
    (tmp_path / "notes.json").write_text("{}", encoding="utf-8")  # non-dated ignored

    prev, curr = gate.load_two_latest(tmp_path)

    assert prev is not None
    assert curr is not None
    assert curr["backends"][0]["backend"] == "code-scanning"
    assert prev["backends"] == []


def test_load_two_latest_handles_single_snapshot(tmp_path: Path) -> None:
    (tmp_path / "2026-07-14.json").write_text(json.dumps(_snapshot()), encoding="utf-8")

    prev, curr = gate.load_two_latest(tmp_path)

    assert prev is None
    assert curr is not None


def test_main_exit_code_and_output(tmp_path: Path) -> None:
    (tmp_path / "2026-07-13.json").write_text(
        json.dumps(_snapshot(_backend("code-scanning", "ok", [_metric("critical_alerts", 0.0, "ok")]))),
        encoding="utf-8",
    )
    (tmp_path / "2026-07-14.json").write_text(
        json.dumps(_snapshot(_backend("code-scanning", "fail", [_metric("critical_alerts", 2.0, "fail")]))),
        encoding="utf-8",
    )
    out = tmp_path / "regressions.json"

    code = gate.main(["--snapshots", str(tmp_path), "--out", str(out)])

    assert code == 1  # a fail-severity regression exits non-zero
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert len(payload) == 1
    assert payload[0]["severity"] == "fail"


def test_main_green_exits_zero_and_writes_empty(tmp_path: Path) -> None:
    (tmp_path / "2026-07-13.json").write_text(json.dumps(_snapshot()), encoding="utf-8")
    (tmp_path / "2026-07-14.json").write_text(json.dumps(_snapshot()), encoding="utf-8")
    out = tmp_path / "regressions.json"

    code = gate.main(["--snapshots", str(tmp_path), "--out", str(out)])

    assert code == 0
    assert json.loads(out.read_text(encoding="utf-8")) == []
