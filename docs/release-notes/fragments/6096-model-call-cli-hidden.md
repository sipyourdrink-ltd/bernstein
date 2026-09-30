## Model-call CLI commands are now test scaffolding only

The `bernstein cost model-call invoke` and `replay` commands are marked
`hidden=True` and fail closed with a clear error message. Real adapter invocation
requires a running agent session managed by the orchestrator. The commands were
added in #6096 but shipped with hardcoded mock output instead of real adapter
integration. Test code should use the `ModelCallLedger` library API directly with
mock callables as needed.
