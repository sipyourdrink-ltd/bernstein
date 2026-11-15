"""Declared coverage survives gate execution, reports, and cache reloads."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from bernstein.core.models import Complexity, Scope, Task

from bernstein.core.quality.gate_pipeline import GatePipelineStep, GateResult, VerificationScope
from bernstein.core.quality.gate_plugins import GatePlugin
from bernstein.core.quality.gate_runner import GateRunner
from bernstein.core.quality.quality_gates import QualityGatesConfig


def _scope() -> VerificationScope:
    return VerificationScope(
        oracle_id="property-oracle-v1",
        kind="test_run",
        checked=("property:stable-output",),
        cannot_check=("src/unrelated.py",),
        confidence="partial",
        evidence_ref="sha256:declared-evidence",
    )


def _task() -> Task:
    return Task(
        id="scope-task",
        title="Check output stability",
        description="Keep the oracle declaration intact.",
        role="backend",
        scope=Scope.MEDIUM,
        complexity=Complexity.MEDIUM,
        owned_files=["src/unrelated.py"],
    )


class _ScopedGate(GatePlugin):
    @property
    def name(self) -> str:
        return "scoped_check"

    def run(self, changed_files: list[str], run_dir: Path, task_title: str, task_description: str) -> GateResult:
        return GateResult(
            name=self.name,
            status="pass",
            required=False,
            blocked=False,
            cached=False,
            duration_ms=0,
            details="stable output",
            scope=_scope(),
        )


def test_plugin_scope_survives_execution_and_persisted_report(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src/unrelated.py").write_text("value = 1\n", encoding="utf-8")
    step = GatePipelineStep(name="scoped_check", required=True)
    runner = GateRunner(QualityGatesConfig(pipeline=[step], cache_enabled=False), tmp_path)
    runner._plugin_registry().register(_ScopedGate())

    report = asyncio.run(runner.run_all(_task(), tmp_path))

    assert report.overall_pass
    assert report.results[0].required
    assert report.results[0].scope == _scope()
    raw = json.loads((tmp_path / ".sdd/runtime/gates/scope-task.json").read_text())
    assert raw["results"][0]["scope"] == _scope().to_dict()
    assert "src/unrelated.py" not in report.results[0].scope.checked


@pytest.mark.parametrize("required", [True, False])
def test_plugin_inconclusive_retains_reason_scope_and_blocking(tmp_path: Path, required: bool) -> None:
    class InconclusiveGate(_ScopedGate):
        def run(self, changed_files: list[str], run_dir: Path, task_title: str, task_description: str) -> GateResult:
            result = super().run(changed_files, run_dir, task_title, task_description)
            return replace(result, status="inconclusive", reason="runner-died-before-output")

    runner = GateRunner(QualityGatesConfig(cache_enabled=False), tmp_path)
    runner._plugin_registry().register(InconclusiveGate())

    result = asyncio.run(runner.run_gate(GatePipelineStep("scoped_check", required=required), _task(), tmp_path, []))

    assert result.status == "inconclusive"
    assert result.reason == "runner-died-before-output"
    assert result.blocked is required
    assert result.scope == _scope()


@pytest.mark.parametrize("declared_scope", [_scope(), None])
def test_scope_survives_cache_reload_without_fabricating_legacy_coverage(
    tmp_path: Path, declared_scope: VerificationScope | None
) -> None:
    (tmp_path / "module.py").write_text("value = 1\n", encoding="utf-8")
    config = QualityGatesConfig(cache_enabled=True)
    step = GatePipelineStep("lint", required=True)
    original = GateResult(
        name="lint",
        status="pass",
        required=True,
        blocked=False,
        cached=False,
        duration_ms=0,
        details="checked declared properties",
        scope=declared_scope,
    )
    execute = AsyncMock(return_value=original)
    with patch.object(GateRunner, "_execute_gate", execute):
        first = asyncio.run(GateRunner(config, tmp_path).run_gate(step, _task(), tmp_path, ["module.py"]))
        second = asyncio.run(GateRunner(config, tmp_path).run_gate(step, _task(), tmp_path, ["module.py"]))

    assert execute.await_count == 1
    assert not first.cached
    assert second.cached
    assert second.scope == declared_scope


def test_legacy_cache_entry_without_scope_remains_unattested(tmp_path: Path) -> None:
    runner = GateRunner(QualityGatesConfig(), tmp_path)
    runner._cache_loaded = True
    runner._cache_entries["legacy"] = {
        "name": "lint",
        "status": "pass",
        "required": True,
        "blocked": False,
        "duration_ms": 0,
        "details": "old cache",
    }

    restored = runner._get_cached_result_sync("legacy")

    assert restored is not None
    assert restored.scope is None


@pytest.mark.parametrize("bad_scope", ["invalid", {}, {**_scope().to_dict(), "confidence": "invalid"}])
def test_invalid_cached_scope_requires_a_fresh_execution(tmp_path: Path, bad_scope: object) -> None:
    runner = GateRunner(QualityGatesConfig(), tmp_path)
    runner._cache_loaded = True
    runner._cache_entries["invalid"] = {
        "name": "lint",
        "status": "pass",
        "required": True,
        "blocked": False,
        "duration_ms": 0,
        "details": "cached verdict",
        "scope": bad_scope,
    }

    assert runner._get_cached_result_sync("invalid") is None


@pytest.mark.parametrize(
    "invalid_fields",
    [
        {"checked": "src/module.py"},
        {"checked": {"src/module.py": True}},
        {"checked": [42]},
        {"cannot_check": "unknown"},
        {"cannot_check": [False]},
        {"oracle_id": ["oracle"]},
        {"kind": 42},
        {"evidence_ref": {"hash": "evidence"}},
    ],
)
def test_malformed_scope_in_disk_cache_executes_gate_again(tmp_path: Path, invalid_fields: dict[str, object]) -> None:
    (tmp_path / "module.py").write_text("value = 1\n", encoding="utf-8")
    config = QualityGatesConfig(cache_enabled=True)
    step = GatePipelineStep("lint", required=True)
    result = GateResult("lint", "pass", True, False, False, 0, "fresh", scope=_scope())
    execute = AsyncMock(return_value=result)
    with patch.object(GateRunner, "_execute_gate", execute):
        asyncio.run(GateRunner(config, tmp_path).run_gate(step, _task(), tmp_path, ["module.py"]))
        cache_path = tmp_path / ".sdd/caching/gate_cache.json"
        entries = json.loads(cache_path.read_text())
        for entry in entries.values():
            entry["scope"].update(invalid_fields)
        cache_path.write_text(json.dumps(entries), encoding="utf-8")

        restored = asyncio.run(GateRunner(config, tmp_path).run_gate(step, _task(), tmp_path, ["module.py"]))

    assert execute.await_count == 2
    assert not restored.cached
    assert restored.scope == _scope()
