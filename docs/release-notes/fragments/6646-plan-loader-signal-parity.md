## Preserve declared plan completion gates

Plan loading, manual validation, exported JSON Schema, and TaskCreate now
share the existing eleven native completion-signal types. Artifact gates
survive plan loading and the task wire instead of being silently dropped.
Per-step CLI overrides are recognized alongside model and effort.

Malformed declared containers, entries, types, and values fail before dispatch;
direct loading raises `PlanLoadError` with stage, step, and signal context.
Existing path/command aliases and `file_contains` forms remain supported.
Bundled examples now use the supported `test_passes`/`command` spelling.
