## `scenario` commands work from a pip install and report what they spawned

The packaged scenarios were located by walking up from the module to a
source-checkout directory and were not in the wheel, so on an installed copy
`bernstein scenario list` printed "No scenarios found." and `scenario run
docs-sync` failed with "Unknown scenario". The scenarios now ship inside the
package and are resolved from it (with the repository `templates/scenarios`
directory as the checkout fallback), for `scenario`, `routine` and the MCP
scenario tools alike. `scenario list` no longer creates `.sdd/routines` in the
working directory.

`scenario run --json` returned before posting anything and printed an empty
`task_ids`. It now spawns the tasks and reports their ids. `scenario run`
exits non-zero, printing "Spawned N of M tasks", when the task server is
unreachable or rejects any task, instead of printing "Successfully spawned 0
tasks" and exiting 0.
