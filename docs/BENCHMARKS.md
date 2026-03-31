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

## Baseline Results (2026-03-31)

### 1. Task Store (`bench_task_store.py`)
Measures raw throughput of the JSONL-backed task store.
- **Creations/sec:** > 10,000
- **Flush Latency (Avg):** 0.15 ms
- **Recovery Time (1k tasks):** < 50 ms

### 2. Orchestrator Latency (`bench_orchestrator.py`)
Measures `Orchestrator.tick()` execution time with a 100-task backlog.
- **Average Tick Latency:** ~2,900 ms
- **Max Tick Latency:** ~3,300 ms
- *Note: Latency is dominated by agent spawning (10 parallel agents). In idle state, latency drops to < 100ms.*

### 3. Quality Gates (`bench_quality_gates.py`)
Measures `verify_task` latency with increasing number of completion signals.
- **1 Signal:** 0.006 ms
- **10 Signals:** 0.06 ms
- **50 Signals:** 0.32 ms
- **100 Signals:** 0.63 ms
- *Scaling is linear with signal count.*

### 4. Startup Latency (`bench_startup.py`)
Measures end-to-end time from orchestrator initialization to completion of the first tick.
- **Average Startup Latency:** 78.15 ms

---

## Performance Targets (Q2 2026)
- [ ] Reduce Orchestrator tick overhead by optimizing signal file polling.
- [ ] Implement bulk claim/complete in a single HTTP request to reduce RTT.
- [ ] Goal: < 500ms tick latency with 10 active agents.
