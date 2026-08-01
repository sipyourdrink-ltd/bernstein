#!/usr/bin/env python3
"""Measure the tool-call interlock at the live MCP gateway boundary.

This intentionally times complete ``MCPGateway.handle_jsonrpc`` dispatches
under bounded parallelism.  It compares an unwired gateway with an enforced
gateway using an in-process evidence provider, so the delta isolates host-seam
overhead without pretending to measure a future signer or durable store.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any

from bernstein.core.persistence.wal import WALWriter
from bernstein.core.protocols.mcp.mcp_gateway import MCPGateway
from bernstein.core.security.toolcall_interlock import (
    AttestationMode,
    ToolCallAttestationInterlock,
    ToolCallIntent,
    VerifiedDispatchEvidence,
)


class _InProcessEvidenceProvider:
    async def prepare_dispatch(self, intent: ToolCallIntent) -> VerifiedDispatchEvidence:
        return VerifiedDispatchEvidence(
            attestation_ref=f"attestation:{intent.span_id}",
            dispatch_ref=f"dispatch:{intent.span_id}",
            intent_digest=intent.digest(),
        )


class _BenchmarkGateway(MCPGateway):
    async def _send_upstream(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        future = self._pending[request_id]
        future.set_result({"jsonrpc": "2.0", "id": request_id, "result": {"ok": True}})


async def _measure_once(*, enforced: bool, calls: int, parallel: int) -> dict[str, float]:
    with tempfile.TemporaryDirectory(prefix="bernstein-interlock-bench-") as temporary:
        sdd_dir = Path(temporary) / ".sdd"
        sdd_dir.mkdir()
        interlock = None
        if enforced:
            interlock = ToolCallAttestationInterlock(
                provider=_InProcessEvidenceProvider(),
                scope_id="scope:benchmark:agent-1",
                mode=AttestationMode.ENFORCED,
            )
        gateway = _BenchmarkGateway(
            upstream_cmd=[],
            wal_writer=WALWriter(run_id="toolcall-interlock-bench", sdd_dir=sdd_dir),
            server_name="benchmark",
            attestation_interlock=interlock,
        )
        samples_ms: list[float] = []

        async def dispatch(index: int) -> None:
            started = time.perf_counter_ns()
            await gateway.handle_jsonrpc(
                {
                    "jsonrpc": "2.0",
                    "id": index,
                    "method": "tools/call",
                    "params": {"name": "noop", "arguments": {"index": index}},
                }
            )
            samples_ms.append((time.perf_counter_ns() - started) / 1_000_000)

        started = time.perf_counter_ns()
        for offset in range(0, calls, parallel):
            await asyncio.gather(*(dispatch(index) for index in range(offset, min(offset + parallel, calls))))
        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
        samples_ms.sort()
        p95_index = max(0, int(len(samples_ms) * 0.95) - 1)
        return {
            "elapsed_ms": elapsed_ms,
            "per_dispatch_wall_ms": elapsed_ms / calls,
            "p50_call_ms": statistics.median(samples_ms),
            "p95_call_ms": samples_ms[p95_index],
        }


async def run_benchmark(*, calls: int, parallel: int, repetitions: int) -> dict[str, Any]:
    """Return median gateway-level baseline and enforced measurements."""
    if calls <= 0 or parallel <= 0 or repetitions <= 0:
        raise ValueError("calls, parallel, and repetitions must be positive")
    parallel = min(parallel, calls)

    # Warm both import and filesystem paths outside the reported samples.
    await _measure_once(enforced=False, calls=min(calls, parallel), parallel=parallel)
    await _measure_once(enforced=True, calls=min(calls, parallel), parallel=parallel)

    baseline_runs: list[dict[str, float]] = []
    enforced_runs: list[dict[str, float]] = []
    for _ in range(repetitions):
        baseline_runs.append(await _measure_once(enforced=False, calls=calls, parallel=parallel))
        enforced_runs.append(await _measure_once(enforced=True, calls=calls, parallel=parallel))

    def medians(rows: list[dict[str, float]]) -> dict[str, float]:
        return {key: statistics.median(row[key] for row in rows) for key in rows[0]}

    baseline = medians(baseline_runs)
    enforced = medians(enforced_runs)
    return {
        "schema": "bernstein.toolcall-interlock-benchmark/v1",
        "calls": calls,
        "parallel": parallel,
        "repetitions": repetitions,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "provider": "in-process evidence handle; signing and durable-chain storage excluded",
        "baseline": baseline,
        "enforced": enforced,
        "delta_per_dispatch_wall_ms": enforced["per_dispatch_wall_ms"] - baseline["per_dispatch_wall_ms"],
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calls", type=int, default=256)
    parser.add_argument("--parallel", type=int, default=32)
    parser.add_argument("--repetitions", type=int, default=5)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    report = asyncio.run(run_benchmark(calls=args.calls, parallel=args.parallel, repetitions=args.repetitions))
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
