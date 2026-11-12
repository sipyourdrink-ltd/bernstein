# {{ cookiecutter.gate_name }}

Copy `{{ cookiecutter.gate_name }}.py` into the project's `.bernstein/gates/`
directory, or register `{{ cookiecutter.gate_class }}` under the
`bernstein.gates` entry-point group. Do not list it under `plugins:`; that
configuration is for hook plugins and warns when given a gate without hook implementations.

Use a Bernstein build containing the quality-gate template fix in PR #6964.
Bernstein 3.24.1 and earlier cannot load this template's dataclass with deferred
annotations from `.bernstein/gates/` reliably.

Add the gate to `bernstein.yaml`:

```yaml
quality_gates:
  pipeline:
    - name: "{{ cookiecutter.gate_name }}"
      required: true
      condition: any_changed
```

Implement `scan()` to return error and warning counts. The gate returns a
`fail` verdict with `blocked=False`; `GateRunner` applies the pipeline step's
`required` policy. Set `required: false` for an advisory check: a failed scan
is recorded but does not block promotion. The runner also sets the result's
`required` field from the pipeline step, overriding the plugin's result metadata.

Run the generated tests with `python -m pytest -q`.

`GateRunner` writes results, details and metadata to the project root's
`.sdd/runtime/gates/<task_id>.json`. Normal task execution also writes events to
`.sdd/metrics/quality_gates.jsonl`. Return evidence in the result's `details`
and `metadata` instead of writing into `run_dir`, which can be a temporary
agent worktree removed after the task. A direct call to `run()` does not persist
the result.
