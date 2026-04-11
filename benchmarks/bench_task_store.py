"""Benchmark for TaskStore performance and persistence latency."""

import asyncio
import shutil
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from bernstein.core.task_store import TaskStore


async def run_benchmark():
    test_dir = Path("bench_workdir")
    if test_dir.exists():
        shutil.rmtree(test_dir)
    test_dir.mkdir()

    jsonl_path = test_dir / "tasks.jsonl"
    store = TaskStore(jsonl_path=jsonl_path)

    num_tasks = 1000

    print(f"Benchmarking {num_tasks} task operations...")

    # 1. Benchmark Creation
    start_time = time.perf_counter()
    for i in range(num_tasks):
        req: Any = SimpleNamespace(
            title=f"Task {i}",
            description="Benchmark task",
            role="backend",
            priority=2,
            scope="small",
            complexity="low",
            estimated_minutes=5,
            depends_on=[],
            owned_files=[],
            cell_id=None,
            task_type="standard",
            upgrade_details=None,
            model="sonnet",
            effort="normal",
            completion_signals=[],
            slack_context=None,
        )
        await store.create(req)

    create_duration = time.perf_counter() - start_time
    print(f"  Creations: {num_tasks / create_duration:.2f} tasks/sec")

    # 2. Benchmark Claiming
    start_time = time.perf_counter()
    task_ids = []
    for _ in range(num_tasks):
        task = await store.claim_next("backend")
        if task:
            task_ids.append(task.id)

    claim_duration = time.perf_counter() - start_time
    print(f"  Claims: {len(task_ids) / claim_duration:.2f} tasks/sec")

    # 3. Benchmark Completion
    start_time = time.perf_counter()
    for tid in task_ids:
        await store.complete(tid, "completed")

    complete_duration = time.perf_counter() - start_time
    print(f"  Completions: {len(task_ids) / complete_duration:.2f} tasks/sec")

    # 4. Flush Latency
    await store.flush_buffer()  # ensure empty

    # Buffer some and measure flush
    for _ in range(store._BUFFER_MAX - 1):
        await store.create(req)

    start_time = time.perf_counter()
    await store.create(req)  # This triggers flush
    flush_duration = time.perf_counter() - start_time
    print(f"  Flush Latency (buffer size {store._BUFFER_MAX}): {flush_duration * 1000:.2f} ms")

    shutil.rmtree(test_dir)


if __name__ == "__main__":
    asyncio.run(run_benchmark())
