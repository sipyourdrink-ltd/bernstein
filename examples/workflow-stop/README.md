# See which commands run after a workflow node fails

Run this command from the repository root with the project dependencies installed.

```bash
python examples/workflow-stop/demo.py
```

The demo loads [workflow.yaml](workflow.yaml) and runs it with `WorkflowRunner`
in a temporary project. Every command writes a marker when it starts.
`branch-a` and `branch-b` share the first layer. After writing its marker,
`branch-b` exits with code 1.

| Layer | Node | Depends on | Status | Marker created |
| --- | --- | --- | --- | --- |
| 1 | `branch-a` | — | success | yes |
| 1 | `branch-b` | — | failed | yes |
| 2 | `after-a` | `branch-a` | skipped | no |
| 2 | `after-b` | `branch-b` | skipped | no |
| 3 | `after-after-a` | `after-a` | skipped | no |

`after-a` depends only on the successful `branch-a`. It is skipped because a
failure in the first layer stops the entire workflow before the second layer.
The script prints the node results and the marker filenames as JSON, then exits
normally. The workflow's own result is `"succeeded": false`.
