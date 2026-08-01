"""Hermetic contract test for the gateway-level interlock benchmark."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[3] / "scripts" / "bench_toolcall_interlock.py"
_spec = importlib.util.spec_from_file_location("_bench_toolcall_interlock", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
bench: Any = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = bench
_spec.loader.exec_module(bench)


@pytest.mark.asyncio
async def test_parallel_gateway_benchmark_reports_both_paths() -> None:
    report = await bench.run_benchmark(calls=8, parallel=4, repetitions=1)

    assert report["schema"] == "bernstein.toolcall-interlock-benchmark/v1"
    assert report["calls"] == 8
    assert report["parallel"] == 4
    assert report["baseline"]["per_dispatch_wall_ms"] > 0
    assert report["enforced"]["per_dispatch_wall_ms"] > 0
    assert "signing and durable-chain storage excluded" in report["provider"]
