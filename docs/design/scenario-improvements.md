## Scenario Command Improvements and Layered Library Loading

### Problem Statement
Currently, Bernstein scenarios cannot be run end-to-end from the CLI, and scenario tasks always default to code_diff artifact specifications regardless of the intended deliverable. Additionally, there are two separate scenario roots (workspace and packaged) that are not properly layered.

### Solution Overview
1. Add a top-level `bernstein scenario` command group with `list` and `run` subcommands
2. Implement layered scenario library loading where workspace scenarios (.bernstein/scenarios) override packaged ones (templates/scenarios)
3. Add artifact_spec field to ScenarioTaskTemplate and propagate it through the task emission pipeline
4. Update scenario YAML parsing to support the artifact_spec field
5. Ensure the routine command's scenario listing indicates the source root of each scenario

### Detailed Changes

#### 1. CLI Command Structure
Add a new scenario command group in `src/bernstein/cli/main.py`:
```
bernstein scenario list    # List all available scenarios with source indicators
bernstein scenario run <id> # Run a scenario end-to-end, emitting tasks
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
- Returns the orchestration ID and task information

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
- Workspace scenarios (.bernstein/scenarios) properly shadow packaged ones (templates/scenarios)
- The routine command continues to work and shows scenario sources
- All existing tests pass
- New tests verify the functionality