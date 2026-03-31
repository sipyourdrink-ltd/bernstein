"""Benchmark: verify_task latency with various signal counts."""

from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path

from bernstein.core.janitor import verify_task
from bernstein.core.models import CompletionSignal, Task


def run_benchmark():
    tmp_dir = Path(tempfile.mkdtemp())

    counts = [1, 10, 50, 100]
    print("Measuring verify_task latency...")

    for count in counts:
        # Prepare signals
        signals = []
        for i in range(count):
            fpath = tmp_dir / f"signal_{i}.txt"
            fpath.write_text("ok")
            signals.append(CompletionSignal(type="path_exists", value=str(fpath.relative_to(tmp_dir))))

        task = Task(
            id=f"task_{count}",
            title="Bench Task",
            description="...",
            role="backend",
            completion_signals=signals,
        )

        # Warmup
        verify_task(task, tmp_dir)

        latencies = []
        for _ in range(100):
            start = time.perf_counter()
            verify_task(task, tmp_dir)
            end = time.perf_counter()
            latencies.append((end - start) * 1000)

        avg_lat = sum(latencies) / len(latencies)
        print(f"  Signals: {count:3d} | Avg Latency: {avg_lat:.4f} ms")

    shutil.rmtree(tmp_dir)


if __name__ == "__main__":
    run_benchmark()
