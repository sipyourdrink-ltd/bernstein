#!/usr/bin/env python3
"""Benchmark native signed identity against native HMAC-only tool evidence."""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any

from bernstein.core.identity.agent_card import AgentIdentityCard
from bernstein.core.lineage.identity import AgentCard
from bernstein.core.persistence.wal import WALWriter
from bernstein.core.protocols.mcp.mcp_gateway import MCPGateway
from bernstein.core.security.agent_card_signer import generate_ed25519_keypair, sign_agent_card
from bernstein.core.security.audit_chain import AuditChainStore
from bernstein.core.security.identity_spawn_anchor import IdentitySpawnAnchor
from bernstein.core.security.native_toolcall_evidence import NativeToolCallEvidenceProvider
from bernstein.core.security.toolcall_identity import LineageToolCallIdentitySigner
from bernstein.core.security.toolcall_interlock import AttestationMode, ToolCallAttestationInterlock


class _BenchmarkGateway(MCPGateway):
    async def _send_upstream(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        self._pending[request_id].set_result({"jsonrpc": "2.0", "id": request_id, "result": {"ok": True}})


def _seed(chain: AuditChainStore, count: int) -> None:
    for index in range(count):
        chain.log(
            event_type="benchmark.history",
            actor="benchmark",
            resource_type="benchmark",
            resource_id=str(index),
            details={"index": index},
        )


def _signed_provider(chain: AuditChainStore) -> NativeToolCallEvidenceProvider:
    card_private, card_public = generate_ed25519_keypair()
    tool_private, tool_public = generate_ed25519_keypair()
    card = AgentIdentityCard(
        agent_id="bench-agent",
        role="benchmark",
        adapter="benchmark",
        model="none",
        created_at=100,
        expires_at=200,
    )
    signature = sign_agent_card(card, card_private, kid="spawn-key")
    identity = IdentitySpawnAnchor(chain, {"spawn-key": card_public}, clock=lambda: 150).anchor(
        run_id="bench-run",
        card=card,
        signature=signature,
        run_journal_head="journal:fixed",
        tool_signing_card=AgentCard(
            agent_id="bench-agent",
            kid="tool-key",
            public_key_pem=tool_public.decode(),
        ),
    )
    return NativeToolCallEvidenceProvider(
        chain,
        run_identity=identity,
        signer=LineageToolCallIdentitySigner(tool_private.decode(), "tool-key"),
        run_journal_head=lambda: "journal:fixed",
        clock_ns=lambda: 123_000_000_001,
    )


async def _measure(*, signed: bool, mode: AttestationMode, history: int, calls: int, parallel: int) -> dict[str, float]:
    with tempfile.TemporaryDirectory(prefix="bernstein-tool-identity-bench-") as temporary:
        chain = AuditChainStore(Path(temporary) / "audit", key=b"k" * 32)
        _seed(chain, history)
        provider = _signed_provider(chain) if signed else NativeToolCallEvidenceProvider(chain)
        sdd_dir = Path(temporary) / ".sdd"
        sdd_dir.mkdir()
        gateway = _BenchmarkGateway(
            upstream_cmd=[],
            wal_writer=WALWriter(run_id="toolcall-identity-bench", sdd_dir=sdd_dir),
            server_name="benchmark",
            attestation_interlock=ToolCallAttestationInterlock(
                provider=provider,
                scope_id="scope:bench-run:bench-agent",
                mode=mode,
            ),
        )
        samples_ms: list[float] = []

        async def one(index: int) -> None:
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
            await asyncio.gather(*(one(index) for index in range(offset, min(calls, offset + parallel))))
        elapsed_s = (time.perf_counter_ns() - started) / 1_000_000_000
        samples_ms.sort()
        p95 = samples_ms[max(0, int(len(samples_ms) * 0.95) - 1)]
        p50 = statistics.median(samples_ms)
        return {
            "p50_ms": p50,
            "p95_ms": p95,
            "throughput_per_s": calls / elapsed_s,
            "lock_wait_proxy_ms": max(0.0, p95 - p50),
        }


def _measure_cold(history: int, repetitions: int = 5) -> dict[str, float]:
    samples_ms: list[float] = []
    for _ in range(repetitions):
        with tempfile.TemporaryDirectory(prefix="bernstein-tool-identity-cold-") as temporary:
            chain = AuditChainStore(Path(temporary) / "audit", key=b"k" * 32)
            _seed(chain, history)
            started = time.perf_counter_ns()
            _signed_provider(chain)
            samples_ms.append((time.perf_counter_ns() - started) / 1_000_000)
    samples_ms.sort()
    return {
        "p50_ms": statistics.median(samples_ms),
        "p95_ms": samples_ms[max(0, int(len(samples_ms) * 0.95) - 1)],
    }


async def run(*, calls: int, parallel: int, histories: list[int], repetitions: int) -> dict[str, Any]:
    """Return paired measurements at each authenticated-history depth."""

    def medians(measurements: list[dict[str, float]]) -> dict[str, float]:
        return {key: statistics.median(row[key] for row in measurements) for key in measurements[0]}

    rows: list[dict[str, Any]] = []
    for history in histories:
        modes: dict[str, Any] = {}
        for mode in (AttestationMode.ENFORCED, AttestationMode.OBSERVED):
            baseline = medians(
                [
                    await _measure(signed=False, mode=mode, history=history, calls=calls, parallel=parallel)
                    for _ in range(repetitions)
                ]
            )
            signed = medians(
                [
                    await _measure(signed=True, mode=mode, history=history, calls=calls, parallel=parallel)
                    for _ in range(repetitions)
                ]
            )
            modes[mode.value] = {
                "hmac_only": baseline,
                "signed_identity": signed,
                "p95_overhead_ms": signed["p95_ms"] - baseline["p95_ms"],
                "throughput_regression_percent": 100
                * (baseline["throughput_per_s"] - signed["throughput_per_s"])
                / baseline["throughput_per_s"],
            }
        rows.append(
            {
                "history_events": history,
                "cold_signed_construction": _measure_cold(history),
                "modes": modes,
            }
        )
    return {
        "schema": "bernstein.toolcall-identity-gateway-benchmark/v1",
        "calls": calls,
        "parallel": parallel,
        "repetitions": repetitions,
        "thresholds": {"p95_overhead_ms": 1.0, "throughput_regression_percent": 10.0},
        "rows": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calls", type=int, default=128)
    parser.add_argument("--parallel", type=int, default=32)
    parser.add_argument("--histories", type=int, nargs="+", default=[1, 1000, 10000])
    parser.add_argument("--repetitions", type=int, default=5)
    args = parser.parse_args()
    if args.calls < 1 or args.parallel < 1 or args.repetitions < 1 or any(value < 0 for value in args.histories):
        parser.error("calls and parallel must be positive; histories must be non-negative")
    report = asyncio.run(
        run(
            calls=args.calls,
            parallel=min(args.parallel, args.calls),
            histories=args.histories,
            repetitions=args.repetitions,
        )
    )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
