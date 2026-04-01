# Bernstein Performance Benchmarks

This document tracks the performance baseline for Bernstein core components.

## Methodology

Benchmarks are located in the `benchmarks/` directory and can be executed via:
```bash
uv run python benchmarks/<script_name>.py
```

### Environment
- **OS:** macOS 16.4 (Darwin)
- **Python:** 3.14.3
- **Storage:** NVMe SSD

---

## Baseline Results

Treat benchmark numbers as environment-specific snapshots. Always re-run on your hardware before using these as capacity limits.

### 1. Task Store (`bench_task_store.py`)
Measures raw throughput of the JSONL-backed task store.
- **Observed trend:** high write throughput and low flush latency on local SSD setups.

### 2. Orchestrator Latency (`bench_orchestrator.py`)
Measures `Orchestrator.tick()` execution time with a 100-task backlog.
- **Observed trend:** latency is dominated by spawn/external process interactions under load.
- *In idle/no-spawn states, tick latency is substantially lower.*

### 3. Quality Gates (`bench_quality_gates.py`)
Measures `verify_task` latency with increasing number of completion signals.
- **Observed trend:** near-linear scaling as signal count increases.

### 4. Startup Latency (`bench_startup.py`)
Measures end-to-end time from orchestrator initialization to completion of the first tick.
- **Observed trend:** startup is generally fast in local developer environments.

---

## Performance Targets (Q2 2026)
- [ ] Reduce Orchestrator tick overhead by optimizing signal file polling.
- [ ] Implement bulk claim/complete in a single HTTP request to reduce RTT.
- [ ] Goal: < 500ms tick latency with 10 active agents.
