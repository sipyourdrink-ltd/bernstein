## Manager and backlog tasks now keep their completion signals

The planner and the manager each built the `POST /tasks` body field by field, and the manager dropped `completion_signals` and `metadata.context_files`; the backlog path parsed `janitor_signals` and never sent them. Both orchestration paths now share one `build_task_body` (`core/tasks/task_body.py`), the backlog path serializes signals through the same helper, and a class-level test fails when a new `TaskCreate` field is neither forwarded nor listed as deliberately not forwarded. (#6300)
