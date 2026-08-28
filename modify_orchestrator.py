import re

with open("src/bernstein/core/orchestration/orchestrator.py", "r") as f:
    content = f.read()

new_method = """    def _check_file_overlap(self, batch: list[Task]) -> bool:
        \"\"\"Return True if any file in *batch* is currently owned by an active agent.

        Checks both the in-memory ``_file_ownership`` dict (cross-referenced
        against live agent status) and the persistent ``_lock_manager`` (for
        crash-recovery locks held across process restarts).  Dead agents do not
        block new batches even if they appear in the ownership index.
        \"\"\"
        all_files = [f for task in batch for f in task.owned_files]
        if not all_files:
            return False

        held_by = {}
        lock_timestamps = {}
        conflict = False

        # In-memory ownership check - filters out dead agents explicitly.
        for fpath in all_files:
            owner_id = self._file_ownership.get(fpath)
            if owner_id:
                session = self._agents.get(owner_id)
                if session and session.status == "working":
                    logger.debug(
                        "File %s owned by active agent %s, deferring batch",
                        fpath,
                        owner_id,
                    )
                    held_by[fpath] = owner_id
                    conflict = True

        # Persistent lock check (survives crashes via FileLockManager TTL).
        conflicts = self._lock_manager.check_conflicts(all_files)
        if conflicts:
            for fpath, lock in conflicts:
                logger.debug(
                    "File %s locked by agent %s (task %s), deferring batch",
                    fpath,
                    lock.agent_id,
                    lock.task_id,
                )
                held_by[fpath] = lock.agent_id
                lock_timestamps[lock.agent_id] = lock.locked_at
                conflict = True

        if conflict:
            detector = getattr(self, "_loop_detector", None)
            if detector:
                waiting_agent = None
                if batch and batch[0].parent_task_id:
                    for session in self._agents.values():
                        if batch[0].parent_task_id in session.task_ids:
                            waiting_agent = session.id
                            break
                if not waiting_agent:
                    waiting_agent = batch[0].id
                    
                detector.record_lock_wait(
                    waiting_agent_id=waiting_agent,
                    wanted_files=all_files,
                    held_by=held_by,
                    lock_timestamps=lock_timestamps if lock_timestamps else None,
                )
            return True

        return False"""

content = re.sub(
    r'    def _check_file_overlap\(self, batch: list\[Task\]\) -> bool:.*?        return False',
    new_method,
    content,
    flags=re.DOTALL
)

with open("src/bernstein/core/orchestration/orchestrator.py", "w") as f:
    f.write(content)
