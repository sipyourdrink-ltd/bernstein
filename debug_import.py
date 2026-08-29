#!/usr/bin/env python3
"""Test if there's a module caching issue."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent / "src"))

import tempfile
import time as _time
from bernstein.core.memory.jsonl_log import JSONLMemoryLog

# First, import the module
from bernstein.core.agents import spawn_prompt

def test():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        
        log = JSONLMemoryLog(root=tmp_path / ".bernstein" / "memory")
        
        now = _time.time()
        horizon = 7 * 24 * 3600
        
        # Write entry at horizon boundary
        log.write("lessons", {"timestamp": now - horizon, "lesson": "boundary lesson"})
        
        # Check the log file content
        log_file = tmp_path / ".bernstein" / "memory" / "lessons.jsonl"
        print(f"Log file content: {log_file.read_text()!r}")
        
        # Read back
        entries = log.read("lessons")
        print(f"Entries from log.read: {entries!r}")
        
        # Check what's in spawn_prompt module
        print(f"\nspawn_prompt._MEMORY_LESSONS_KEY = {spawn_prompt._MEMORY_LESSONS_KEY!r}")
        print(f"spawn_prompt.SPAWN = {spawn_prompt.SPAWN}")
        print(f"spawn_prompt.SPAWN.memory_lessons_horizon_s = {spawn_prompt.SPAWN.memory_lessons_horizon_s}")
        
        # Check what log.read returns for the spawn_prompt key
        entries2 = spawn_prompt.JSONLMemoryLog(
            root=tmp_path / ".bernstein" / "memory"
        ).read(spawn_prompt._MEMORY_LESSONS_KEY)
        print(f"Entries from spawn_prompt's JSONLMemoryLog: {entries2!r}")
        
        # Call the function
        block = spawn_prompt._render_memory_lessons_block(tmp_path)
        print(f"\nFunction returned: {block!r}")

if __name__ == "__main__":
    test()