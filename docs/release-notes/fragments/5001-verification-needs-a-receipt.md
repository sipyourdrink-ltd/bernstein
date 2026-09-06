## A completion that claims a verification ran must name where it was recorded

`verification` and `receipt_ref` were validated as two independent optional
fields on `worker-completion/v1`, so a completion could assert that a command
ran and exited 0 while pointing at no receipt at all:

```python
parse_terminal_payload({
    "contract": "worker-completion/v1",
    "summary": "ran the suite, all green",
    "verification": {"command": "pytest -q", "exit_code": 0},
})   # parsed, receipt_ref=None
```

A claim backed by a receipt and one the worker simply asserted serialized
identically, so nothing reading the record later — the orchestrator deciding
whether a dependent task may start, an operator reading the ledger — could tell
them apart.

The two are now dependent at the boundary: a payload carrying `verification`
with no `receipt_ref` raises `ContractViolation` with `path == "$.receipt_ref"`,
naming the half that has to be supplied so the failure is diagnosable from the
ledger without the original payload. A completion carrying neither field is
still valid, and so is `receipt_ref` alone (#5001).
