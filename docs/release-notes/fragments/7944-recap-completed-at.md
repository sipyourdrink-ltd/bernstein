## Offline `recap --since` counts tasks by their completion time

`bernstein recap --since` filtered `.sdd/runtime/tasks.jsonl` by a `timestamp`
field, while the TaskStore records completion time as `completed_at`, so recent
completed and failed tasks counted zero. The recap now uses `completed_at`, and
still accepts `timestamp` on older rows (#7944).
