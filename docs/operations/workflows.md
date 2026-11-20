# Workflow manifests

**How do I chain agent and shell steps into a repeatable pipeline without
writing a plan YAML by hand each time?**

`bernstein workflow` runs declarative YAML manifests: a small DAG of
`agent` and `command` nodes with dependencies, optional retry loops, and
a `{goal}` placeholder substituted at run time. Two schema flavours are
auto-detected from the same command group - the current YAML manifest
(`nodes:` as a list) and a legacy conditional DAG DSL (`phases:` +
`nodes:` as a mapping) kept for backward compatibility.

---

## TL;DR

| Command | Does |
|---|---|
| `bernstein workflow list` | List bundled + project + user manifests. |
| `bernstein workflow run <name> -g "<goal>"` | Execute a manifest. |
| `bernstein workflow run <name> --dry-run` | Print the execution plan only. |
| `bernstein workflow validate <file>` | Validate a manifest (either schema). |
| `bernstein workflow init <name>` | Scaffold a new manifest. |
| `bernstein workflow show <name>` | Inspect a legacy DSL file's phases/edges. |
| `bernstein workflow resume <run_id>` | Resume a killed or interrupted workflow run. |

Manifests resolve from three places, project-local wins:

1. `<project>/.bernstein/workflows/*.yaml`
2. `~/.bernstein/workflows/*.yaml`
3. Bundled `templates/workflows/*.yaml`

---

## Writing a manifest

```yaml
name: idea-to-pr
description: "Take a goal from idea to merged PR."
version: "1.0.0"

nodes:
  - id: research
    agent: manager
    prompt: "Research the goal: {goal}"

  - id: plan
    depends_on: [research]
    agent: architect
    prompt: "Turn the research brief into a concrete plan for: {goal}"

  - id: implement
    depends_on: [plan]
    agent: backend
    prompt: "Carry out the plan for: {goal}"
    fresh_context: true
    timeout_seconds: 3600

  - id: tests
    depends_on: [implement]
    command: "pytest -x -q"
    timeout_seconds: 1800
```

Node fields (`core/workflows/workflow_spec.py`, `WorkflowNode`):

| Field | Meaning |
|---|---|
| `id` | Slug-shaped, unique within the manifest. |
| `depends_on` | Ids that must finish before this node starts. |
| `command` | Bash command (mutually exclusive with `agent`). |
| `agent` | Role/spec name that dispatches through `AgentSpawner` (requires `prompt`). |
| `prompt` | Prompt body; `{goal}` is substituted from `run(goal=...)`. |
| `loop` | `{until: "<bash predicate>", max_iterations: N}` - re-fires the node until the predicate exits 0 or the cap is hit. |
| `fresh_context` | Agent nodes only: no session carryover between loop iterations. |
| `timeout_seconds` | Per-iteration wall-clock cap (1-86400, default set in `workflow_spec.py`). |
| `interactive` | **Rejected at load time** - see [Limitations](#limitations). |

Bundled examples ship under `templates/workflows/`: `idea-to-pr.yaml`,
`refactor-with-tests.yaml`, `doc-update.yaml`, `hot-fix.yaml`,
`security-review.yaml`, `dependency-bump.yaml`.

## Running

```bash
bernstein workflow run idea-to-pr -g "Add JWT auth"
bernstein workflow run ./templates/workflows/refactor-with-tests.yaml
bernstein workflow run idea-to-pr --dry-run   # print topological layers, don't execute
```

The runner (`core/workflows/workflow_runner.py`, `WorkflowRunner`)
executes the manifest as a topological DAG: every layer of nodes whose
dependencies are satisfied runs concurrently via a thread pool.
Agent-typed nodes dispatch through the existing `AgentSpawner`;
command-typed nodes shell out with `subprocess.run`. Each node's
terminal status is one of `success`, `failed`, or `skipped`. A node is
skipped when its own `when` predicate is false, or when an upstream
dependency failed. Once any node fails, the run aborts: every node in a
later layer is skipped and nothing in it is dispatched, even on a branch
that did not fail. A node with `loop:` re-fires until
its `until` bash predicate exits `0` or `max_iterations` is reached, at
which point the node records a failure if its predicate still has not passed.

`bernstein workflow run` exits non-zero if any node ends up `failed`.

## Resuming workflows

Agent dispatch currently does not wait for verified completion before releasing
dependent nodes. The [completion boundary proposal](../architecture/workflow-agent-completion-proposal.md)
records the unresolved execution and resume guarantees tracked in #6639.

Workflow runs can be resumed after interruption (Ctrl+C, crash, kill) using:

```bash
bernstein workflow run idea-to-pr -g "Add JWT auth"  # prints run_id
bernstein workflow resume <run_id> -g "Add JWT auth"
bernstein workflow resume <run_id> -m ./my-flow.yaml -g "Add JWT auth"
```

Pass the original goal again when unfinished nodes use `{goal}`, including
when supplying `-m`. The resume command defaults `--goal` to an empty string;
it does not restore the goal from `spec_snapshot.json`.

The resume command:
- Validates that the provided manifest matches the one used at run start via spec digest
- Loads persisted state from `runs/<run_id>/` under the workflow working directory
- Skips already-completed nodes
- Continues execution from the first non-completed node
- Preserves the original total iteration limit when resuming a command loop

The CLI stores checkpoints under `runs/<run_id>/` relative to the directory where it was run. The Python runner uses its configured working directory.
- `spec_snapshot.json` - state format `version`, `spec_name`, `spec_version`,
  `spec_digest`, `node_ids`, and `source` (manifest path or name); no goal text
- `<node_id>.node.json` - checkpoint for each completed node
- `<node_id>.loop.json` - progress for a command loop, including the current iteration and its last durable decision
- `command_loops.json` - binds a new run’s command-loop checkpoint format to its manifest
- `run_complete.json` - sentinel written when the run finishes

**Spec digest validation**: On resume, the runner computes a digest of the
provided manifest and compares it to the one recorded at run start. A mismatch
is refused to prevent executing different manifests with the same run ID.

A command loop checkpoints its outer `when` decision, each successful command,
and the decision made by `until`. Resuming continues at the next unstarted
step. The original `max_iterations` applies across all starts and resumes.
A successful stop, a command failure, a predicate timeout or exhaustion retains
its outcome even if interruption preceded the ordinary terminal node checkpoint.

If the run stopped after a command or condition was marked as started but
before its outcome was saved, both runner entry points stop before executing
another node. Review the command’s or condition’s external effects and the
retained results before starting a new run. An older run with no loop
checkpoint can start a loop when earlier checkpoints prove it was never entered.
If the available records cannot establish that, the same review is required.
A completed node’s ordinary checkpoint remains authoritative.

## Validating and inspecting

```bash
bernstein workflow validate templates/workflows/idea-to-pr.yaml
bernstein workflow list
bernstein workflow list --bundled-only
bernstein workflow init my-flow
bernstein workflow init my-flow --target ~/workflows/my-flow.yaml --force
```

`validate` sniffs the file structurally: `phases:` at the top level
means legacy DSL, a top-level `nodes:` list means the current manifest
schema. The non-matching path imports no schema, so a malformed DSL file
can't crash the manifest validator or vice versa. `init` scaffolds a
blank manifest and round-trips it through the parser before writing, so
the scaffold can never fail its own `validate`.

## Legacy DSL

`bernstein workflow show <name>` inspects the older conditional-DAG DSL
(`core/planning/workflow_dsl.py`): nodes are keyed by id under a mapping
(not a list), grouped into named `phases:` with `allowed_roles` and
optional `requires_approval`, and edges may carry guard conditions. DSL
files live under `.bernstein/workflows/` and are matched by name only
(no bundled or user-home search path). `workflow list` picks up DSL
files from the same project directory as a best-effort secondary scan;
`workflow validate` and `workflow show` both work against them
directly. New manifests should use the YAML schema above - the DSL path
exists for files written before the YAML manifest schema landed.

## Limitations

- **`interactive: true` is not implemented.** A node with `interactive`
  set is rejected at *load* time with a validation error referencing
  ticket #1110, before any upstream node runs. The runner also carries a
  defence-in-depth `NotImplementedError` for out-of-band loaders that
  bypass the validator. There is currently no human-approval gate inside
  a workflow run.
- **No per-node adapter/model routing.** Nodes inherit the orchestrator's
  default adapter and model (`bernstein.yaml` top-level `cli:` + role
  policy). To pin a different CLI or model for one step, lift the
  manifest into a plan YAML and use per-step routing instead - see
  [Per-step CLI and model routing](../workflows/per-step-routing.md).
- **Bash-only loop predicates and command nodes.** There is no built-in
  retry/backoff policy beyond `loop.max_iterations`, and command nodes
  have no sandboxing beyond the orchestrator's own worktree isolation.

## Source

- `src/bernstein/cli/commands/workflow_cmd.py` - CLI group.
- `src/bernstein/core/workflows/workflow_spec.py` - manifest schema, discovery, resolution.
- `src/bernstein/core/workflows/workflow_runner.py` - DAG execution.
- `src/bernstein/core/planning/workflow_dsl.py` - legacy conditional DAG DSL.
- `templates/workflows/` - bundled manifests.