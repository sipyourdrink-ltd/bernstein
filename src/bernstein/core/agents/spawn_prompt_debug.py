"""Debug version of _render_memory_lessons_block with prints."""

import fnmatch
import logging
import os
import re as _re
import shlex as _shlex
import subprocess as _subprocess
import time as _time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bernstein.core.agents import project_context as _project_context
from bernstein.core.agents.heartbeat import HeartbeatMonitor
from bernstein.core.agents.project_context import resolve_project_context
from bernstein.core.context_recommendations import RecommendationEngine
from bernstein.core.defaults import SPAWN
from bernstein.core.lessons import gather_lessons_for_context
from bernstein.core.memory.jsonl_log import JSONLMemoryLog
from bernstein.templates.renderer import TemplateError, render_role_prompt

if TYPE_CHECKING:
    from pathlib import Path


#: JSONL key under ``.bernstein/memory/`` that the spawner reads when
#: auto-injection is enabled.
_MEMORY_LESSONS_KEY = "lessons"

#: Cap on the number of recent entries injected into the prompt.
_MEMORY_LESSONS_MAX = 10

#: Stable separator used to demarcate the lessons block.
_MEMORY_LESSONS_OPEN = "<lessons>"
_MEMORY_LESSONS_CLOSE = "</lessons>"


def _format_memory_lesson(entry: dict[str, Any]) -> str:
    """Render a single memory entry as a compact bullet."""
    text = entry.get("lesson") or entry.get("text") or entry.get("message")
    if not text:
        try:
            import json as _json
            return f"- {_json.dumps(entry, ensure_ascii=False, sort_keys=True)}"
        except Exception:
            return ""
    task = entry.get("task")
    if task:
        return f"- ({task}) {text}"
    return f"- {text}"


def _render_memory_lessons_block_debug(workdir: Path) -> str:
    """Debug version with prints."""
    print(f"\n=== DEBUG _render_memory_lessons_block ===")
    print(f"workdir = {workdir}")
    
    root = workdir / ".bernstein" / "memory"
    print(f"root = {root}")
    
    log = JSONLMemoryLog(root=root)
    print(f"log = {log}")
    
    try:
        entries = log.read(_MEMORY_LESSONS_KEY)
        print(f"entries = {entries!r}")
    except Exception as exc:
        print(f"Exception: {exc}")
        return ""
    
    if not entries:
        print("entries is empty - returning empty")
        return ""
    
    now = _time.time()
    print(f"now = {now}")
    print(f"SPAWN.memory_lessons_horizon_s = {SPAWN.memory_lessons_horizon_s}")
    
    # 1. Age bounding - filter entries older than horizon
    horizon = SPAWN.memory_lessons_horizon_s
    print(f"horizon = {horizon}")
    
    recent_entries = []
    for entry in entries:
        ts = entry.get("timestamp", 0)
        age = now - ts
        print(f"  Entry ts={ts}, age={age}, age <= horizon? {age <= horizon}")
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
    
    print(f"After age bounding: {len(recent_entries)} entries")
    
    # 2. Sort by (weight desc, recency desc) for deterministic output
    recent_entries.sort(key=lambda e: (-e["_weight"], -e.get("timestamp", 0)))
    
    # 3. Per-author cap - keep only N entries per author
    author_entries = {}
    for entry in recent_entries:
        author = entry.get("author", "")
        if author not in author_entries:
            author_entries[author] = []
        author_entries[author].append(entry)
    
    capped_entries = []
    for author, entries_list in author_entries.items():
        capped_entries.extend(entries_list[:SPAWN.memory_lessons_max_per_author])
    
    # 4. Limit to max entries overall
    final_entries = capped_entries[:_MEMORY_LESSONS_MAX]
    print(f"After all filtering: {len(final_entries)} entries")
    
    bullets: list[str] = []
    for entry in final_entries:
        rendered = _format_memory_lesson(entry)
        print(f"  Rendered: '{rendered}'")
        if rendered:
            bullets.append(rendered)
    
    if not bullets:
        print("No bullets - returning empty!")
        return ""
    
    body = "\n".join(bullets)
    result = f"\n{_MEMORY_LESSONS_OPEN}\n{body}\n{_MEMORY_LESSONS_CLOSE}\n"
    print(f"Returning: {result!r}")
    return result