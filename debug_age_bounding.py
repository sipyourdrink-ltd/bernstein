#!/usr/bin/env python3
"""Debug script to trace age bounding logic step by step."""

import tempfile
import time as _time
from pathlib import Path
from bernstein.core.memory.jsonl_log import JSONLMemoryLog
from bernstein.core.defaults import SPAWN

def debug_step_by_step():
    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = Path(tmpdir)
        root = workdir / ".bernstein" / "memory"
        log = JSONLMemoryLog(root=root)

        now = _time.time()
        horizon = SPAWN.memory_lessons_horizon_s
        print(f"now = {now}")
        print(f"horizon = {horizon}")

        # Write entry exactly at horizon boundary
        ts = now - horizon  # exactly at horizon
        print(f"\nWriting entry with timestamp = {ts}")
        print(f"Age will be: now - ts = {now - ts}")
        print(f"Age <= horizon? {now - ts <= horizon}")

        log.write("lessons", {"timestamp": ts, "lesson": "boundary lesson"})

        # Read back
        print(f"\nReading entries...")
        entries = log.read("lessons")
        print(f"Got {len(entries)} entries")
        for i, e in enumerate(entries):
            print(f"  [{i}] {e}")

        # Now trace through the age bounding logic
        print(f"\n--- Tracing age bounding ---")
        recent_entries = []
        for entry in entries:
            ts_entry = entry.get("timestamp", 0)
            age = now - ts_entry
            print(f"  Entry ts={ts_entry}, age={age}, age <= horizon? {age <= horizon}")
            if age <= horizon:
                # Compute weight
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
            print(f"  {e}")

        # Sort
        recent_entries.sort(key=lambda e: (-e["_weight"], -e.get("timestamp", 0)))
        print(f"\nAfter sorting")

        # Per-author cap
        author_entries = {}
        for entry in recent_entries:
            author = entry.get("author", "")
            if author not in author_entries:
                author_entries[author] = []
            author_entries[author].append(entry)

        capped_entries = []
        for author, entries_list in author_entries.items():
            capped_entries.extend(entries_list[:SPAWN.memory_lessons_max_per_author])

        print(f"\nAfter per-author cap: {len(capped_entries)} entries")

        # Limit to max
        from bernstein.core.spawn_prompt import _MEMORY_LESSONS_MAX, _format_memory_lesson
        final_entries = capped_entries[:_MEMORY_LESSONS_MAX]
        print(f"\nAfter global cap: {len(final_entries)} entries")

        bullets = []
        for entry in final_entries:
            rendered = _format_memory_lesson(entry)
            print(f"  Rendered: '{rendered}'")
            if rendered:
                bullets.append(rendered)

        print(f"\nBullets: {bullets}")

        if not bullets:
            print("No bullets - block will be empty!")
        else:
            body = "\n".join(bullets)
            from bernstein.core.spawn_prompt import _MEMORY_LESSONS_OPEN, _MEMORY_LESSONS_CLOSE
            block = f"\n{_MEMORY_LESSONS_OPEN}\n{body}\n{_MEMORY_LESSONS_CLOSE}\n"
            print(f"\nFinal block: {block}")

if __name__ == "__main__":
    debug_step_by_step()