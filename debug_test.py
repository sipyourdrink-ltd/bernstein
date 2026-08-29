#!/usr/bin/env python3
"""Debug the exact test case."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent / "src"))

import tempfile
import time as _time
from bernstein.core.memory.jsonl_log import JSONLMemoryLog
from bernstein.core.spawn_prompt import (
    _render_memory_lessons_block,
    _MEMORY_LESSONS_KEY,
)

def debug_test():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        
        log = JSONLMemoryLog(root=tmp_path / ".bernstein" / "memory")
        
        now = _time.time()
        horizon = 7 * 24 * 3600  # 7 days
        
        print(f"now = {now}")
        print(f"horizon = {horizon}")
        print(f"ts = {now - horizon}")
        print(f"tmp_path = {tmp_path}")
        
        # Write entry exactly at horizon (now - horizon)
        log.write(_MEMORY_LESSONS_KEY, {"timestamp": now - horizon, "lesson": "boundary lesson"})
        
        print(f"\nWritten to: {tmp_path / '.bernstein' / 'memory' / 'lessons.jsonl'}")
        
        # Check the file exists
        log_file = tmp_path / ".bernstein" / "memory" / "lessons.jsonl"
        print(f"Log file exists: {log_file.exists()}")
        if log_file.exists():
            print(f"Contents: {log_file.read_text()}")
        
        # Read back
        entries = log.read(_MEMORY_LESSONS_KEY)
        print(f"\nRead {len(entries)} entries")
        for e in entries:
            print(f"  {e}")
        
        # Call the function under test
        block = _render_memory_lessons_block(tmp_path)
        print(f"\nBlock: '{block}'")
        print(f"Contains 'boundary lesson'? {'boundary lesson' in block}")

if __name__ == "__main__":
    debug_test()