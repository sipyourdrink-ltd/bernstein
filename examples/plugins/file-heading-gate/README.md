# Check a report heading with a gate plugin

Run the demo from the repository root with the project dependencies installed.

```bash
python examples/plugins/file-heading-gate/demo.py
```

The demo copies [file_heading_gate.py](file_heading_gate.py) into a temporary
project's `.bernstein/gates/` directory. `GateRunner` discovers the plugin
there. The plugin checks whether `doc.md` contains the exact line
`# Status Report` and returns its verdict with `blocked=False`. The Python
pipeline selects the plugin by name with
`GatePipelineStep(name="file_heading", required=required)`.

The JSON output shows how the step's `required` setting affects the report.

| File contents | Required | Gate status | Blocked | Overall pass |
| --- | --- | --- | --- | --- |
| `# Status Report` | true | pass | false | true |
| `# Notes` | true | fail | true | false |
| `# Notes` | false | fail | false | true |

To use this gate in a Python pipeline, copy the plugin file into your project's
`.bernstein/gates/` and construct the step as shown in [demo.py](demo.py).
