## Scenario Command Improvements and Layered Library Loading

Status: implemented — `bernstein scenario list/run` (`scenario_cmd.py`, registered in `cli/main.py`), `load_layered_scenario_library`, and `ScenarioTaskTemplate.artifact_spec` exist. A CLI-specific `tests/unit/test_scenario_cmd.py` was not added; `tests/unit/test_routine_bridge.py` covers task emission and skipped failed posts.

### Problem Statement
Before this change, Bernstein scenarios could not be run end-to-end from the CLI, and scenario tasks always defaulted to code_diff artifact specifications regardless of the intended deliverable. Additionally, there were two separate scenario roots (workspace and packaged) that were not properly layered.

### Solution Overview
1. Add a top-level `bernstein scenario` command group with `list` and `run` subcommands
2. Implement layered scenario library loading where workspace scenarios (.bernstein/scenarios) override packaged ones (templates/scenarios)
3. Add artifact_spec field to ScenarioTaskTemplate and propagate it through the task emission pipeline
4. Update scenario YAML parsing to support the artifact_spec field
5. Ensure the routine command's scenario listing indicates the source root of each scenario

### Detailed Changes

#### 1. CLI Command Structure
Add a new scenario command group (defined in `src/bernstein/cli/commands/scenario_cmd.py` and registered in `src/bernstein/cli/main.py`):
```
bernstein scenario list    # List all available scenarios with source indicators
bernstein scenario run <id> # Enqueue a scenario's tasks on the task server
```

#### 2. Layered Scenario Library Loading
Modify `src/bernstein/core/planning/scenario_library.py` to:
- Accept multiple root directories
- Load scenarios from workspace root first (.bernstein/scenarios)
- Overlay packaged scenarios (templates/scenarios) on top, with workspace taking precedence
- Return a unified library where workspace scenarios shadow packaged ones with the same ID

#### 3. Artifact Specification Support
Update `ScenarioTaskTemplate` in `src/bernstein/core/planning/scenario_library.py`:
- Add `artifact_spec: ArtifactSpec = field(default_factory=ArtifactSpec)` field
- Update YAML parsing in `_load_recipe_file` to handle artifact_spec
- Modify `build_task_payloads` in `routine_bridge.py` to include artifact_spec in task payloads

#### 4. Roadmap Runtime Integration
Update `src/bernstein/core/planning/roadmap_runtime.py` to:
- Use the layered scenario library instead of just the workspace root
- Ensure roadmap emission works with the new library structure

#### 5. Routine Bridge Integration
Update `src/bernstein/core/planning/routine_bridge.py` to:
- Use the layered scenario library
- Maintain backward compatibility with existing API

#### 6. Routine Command Enhancement
Update `src/bernstein/cli/commands/routine_cmd.py` to:
- Show which root each scenario came from (workspace or packaged)
- Indicate when a workspace scenario shadows a packaged one

#### 7. Scenario Run Implementation
Create the `scenario run` subcommand that:
- Loads the layered scenario library
- Finds the specified scenario
- Uses RoutineBridge to build task payloads
- Posts tasks to the Bernstein task server
- Reports the orchestration ID and the task IDs returned by the server; it does not wait for task execution to finish

#### 8. Enqueue Result and Run Completion
`scenario run` completes the enqueue operation, not the scenario's work. It POSTs each generated task independently. A task is counted as spawned when the task server response supplies a non-empty task ID. A successful command means every expected task received an ID (and the expected task count is nonzero); it says nothing about whether the tasks have started, succeeded, failed, or finished. Check the ordinary task/run status separately after enqueueing.

With `--json`, the response contains:
- `orchestration_id`: identifier shared by tasks from this invocation
- `scenario_id`: the invoked scenario
- `task_count`: expected number of tasks in the invocation
- `spawned_count`: number of task IDs returned by the server
- `estimated_minutes`: rough total wall-clock estimate, not a wait duration or completion guarantee
- `task_ids`: IDs returned for successfully enqueued tasks, in payload order

The JSON response has no separate success field: compare `spawned_count` with `task_count` and use the process exit code. A complete three-task enqueue returns three task IDs (`task_count: 3`, `spawned_count: 3`) and exits zero; this confirms all posts received IDs, not that any task finished. If one or more posts fail or return no ID, the command reports the partial result and exits nonzero. In JSON mode it still emits the fields above before returning nonzero. Tasks already created are not rolled back; their IDs remain in `task_ids`. The non-JSON output likewise reports the spawned/expected counts and each ID when only some tasks were created, then exits nonzero. An empty scenario is not reported as a successful run.

For example, if a three-task scenario receives IDs for two posts, the enqueue result has `task_count: 3`, `spawned_count: 2`, and two `task_ids`; the command exits nonzero. This is a partial enqueue, not a report that either task has completed or that the two tasks failed.

### Files to Modify
- `src/bernstein/cli/main.py` - Add scenario command group
- `src/bernstein/core/planning/scenario_library.py` - Add artifact_spec, layered loading
- `src/bernstein/core/planning/roadmap_runtime.py` - Use layered library
- `src/bernstein/core/planning/routine_bridge.py` - Use layered library
- `src/bernstein/cli/commands/routine_cmd.py` - Show scenario sources
- `src/bernstein/cli/commands/scenario_cmd.py` (new) - scenario list/run commands
- `tests/unit/test_scenario_cmd.py` (new) - Test scenario commands
- `tests/unit/test_scenario_library.py` (updated) - Test artifact_spec and layered loading

### Acceptance Criteria
- `bernstein scenario list` shows all scenarios with source indicators
- `bernstein scenario run <id>` emits tasks that match the scenario's artifact_spec
- All expected task posts return IDs for a zero exit; an empty scenario does not count as success
- A partial enqueue returns the IDs for created tasks, reports the expected and spawned counts, and exits nonzero without rolling back created tasks
- Run success describes enqueue acceptance only; later task status is checked separately
- Workspace scenarios (.bernstein/scenarios) properly shadow packaged ones (templates/scenarios)
- The routine command continues to work and shows scenario sources
- All existing tests pass