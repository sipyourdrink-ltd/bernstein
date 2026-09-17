## gRPC TaskService now fills `goal` from a real task

`_task_to_proto` and `TaskServiceImpl._fill_task_proto` populated the
proto's `goal` field from a `"goal"` dict key, but the orchestrator's own
`Task` model (and its `to_dict()` output) has no `goal` field at all --
only `title`. Every `CreateTask`/`ClaimTask`/`CompleteTask`/`FailTask`/
`GetTask`/`ListTasks` RPC over a real task therefore returned an empty
`goal`. Both sites now fall back to `title` (the task's short form, not
its full description) when `goal` is absent (#5888).
