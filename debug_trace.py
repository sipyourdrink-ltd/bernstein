#!/usr/bin/env python3
"""Trace through _render_memory_lessons_block step by step."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent / "src"))

import tempfile
import time as _time
import json
from bernstein.core.memory.jsonl_log import JSONLMemoryLog
from bernstein.core.spawn_prompt import (
    _render_memory_lessons_block,
    _MEMORY_LESSONS_KEY,
    _MEMORY_LESSONS_MAX,
    _format_memory_lesson,
)
from bernstein.core.defaults import SPAWN

def trace_through():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        
        log = JSONLMemoryLog(root=tmp_path / ".bernstein" / "memory")
        
        now = _time.time()
        horizon = SPAWN.memory_lessons_horizon_s
        print(f"=== TRACING _render_memory_lessons_block ===")
        print(f"now = {now}")
        print(f"horizon = {horizon}")
        
        # Write entry exactly at horizon (now - horizon)
        ts = now - horizon
        entry = {"timestamp": ts, "lesson": "boundary lesson"}
        log.write(_MEMORY_LESSONS_KEY, entry)
        
        # Read raw JSONL
        log_file = tmp_path / ".bernstein" / "memory" / "lessons.jsonl"
        raw_content = log_file.read_text()
        print(f"\nRaw JSONL content: {raw_content}")
        
        # Read back via log
        entries = log.read(_MEMORY_LESSONS_KEY)
        print(f"\nEntries from log.read(): {entries}")
        
        # Now trace through _render_memory_lessons_block
        print(f"\n--- Step 1: Age bounding ---")
        recent_entries = []
        for entry in entries:
            ts_entry = entry.get("timestamp", 0)
            age = now - ts_entry
            print(f"  Entry: ts={ts_entry}, age={age}, age <= horizon? {age <= horizon}")
            if age <= horizon:
                bucket_size = horizon * SPAWN.memory_lessons_weight_decay_factor
                if bucket_size <= 0:
                    bucket_size = horizon / 2
                bucket = int(age // bucket_size)
                weight = SPAWN.memory_lessons_weight_decay_factor ** bucket
                print(f"    bucket_size={bucket_size}, bucket={bucket}, weight={weight}")
                entry_copy = dict(entry)
                entry_copy["_weight"] = weight
                entry_copy["_age"] = age
                recent_entries.append(entry_copy)
        
        print(f"\nAfter age bounding: {len(recent_entries)} entries")
        for e in recent_entries:
            print(f"  {json.dumps(e)}")
        
        print(f"\n--- Step 2: Sorting ---")
        recent_entries.sort(key=lambda e: (-e["_weight"], -e.get("timestamp", 0)))
        for e in recent_entries:
            print(f"  {json.dumps(e)}")
        
        print(f"\n--- Step 3: Per-author cap ---")
        author_entries = {}
        for entry in recent_entries:
            author = entry.get("author", "")
            if author not in author_entries:
                author_entries[author] = []
            author_entries[author].append(entry)
        print(f"Author entries: {list(author_entries.keys())}")
        for author, entries_list in author_entries.items():
            print(f"  {author}: {len(entries_list)} entries")
        
        capped_entries = []
        for author, entries_list in author_entries.items():
            capped_entries.extend(entries_list[:SPAWN.memory_lessons_max_per_author])
        
        print(f"\nAfter per-author cap: {len(capped_entries)} entries")
        
        print(f"\n--- Step 4: Global cap ---")
        final_entries = capped_entries[:_MEMORY_LESSONS_MAX]
        print(f"After global cap: {len(final_entries)} entries")
        
        print(f"\n--- Step 5: Formatting ---")
        bullets = []
        for entry in final_entries:
            print(f"\nFormatting entry: {entry}")
            rendered = _format_memory_lesson(entry)
            print(f"  Rendered: '{rendered}' (length: {len(rendered)})")
            if rendered:
                bullets.append(rendered)
        
        print(f"\n--- Final result ---")
        print(f"Total bullets: {len(bullets)}")
        print(f"Bullets: {bullets}")
        
        if not bullets:
            print("ERROR: No bullets created!")
            print(f"This means _format_memory_lesson returned empty string for entry: {entry}")
            # Check why _format_memory_lesson might return empty
            print(f"\nChecking _format_memory_lesson logic:")
            text = entry.get("lesson") or entry.get("text") or entry.get("message")
            print(f"  text from entry.get('lesson'): '{entry.get('lesson')}'")
            print(f"  text from entry.get('text'): '{entry.get('text')}'")
            print(f"  text from entry.get('message'): '{entry.get('message')}'")
            print(f"  combined text: '{text}'")
            print(f"  text is falsy? {not text}")
            if not text:
                print("  This is why _format_memory_lesson returned empty!")
        
        # Now call the actual function to compare
        print(f"\n--- Comparing with actual function call ---")
        block = _render_memory_lessons_block(tmp_path)
        print(f"Actual block: '{block}'")

if __name__ == "__main__":
    trace_through()