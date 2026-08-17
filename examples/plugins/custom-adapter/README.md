# custom-adapter

example bernstein adapter plugin: a `claude-mock` adapter that returns
canned, deterministic responses. useful for offline ci, contract tests,
and demos where you want bernstein's full orchestration loop to run
without spending a cent on real model calls.

## install

```bash
pip install -e examples/plugins/custom-adapter
```

bernstein discovers the adapter via the `bernstein.adapters` entry-point
group declared in `pyproject.toml`. once installed, `bernstein run --cli
claude_mock` routes every spawn to the mock.

## what it does

* registers the slug `claude_mock` against the bernstein adapter
  registry.
* on `spawn(...)`, writes a deterministic ndjson stream-json transcript
  to the session log path that's identical across runs (modulo the
  session id baked into the system event), so golden-file tests in
  downstream consumers can pin the output.
* exits 0 immediately - bernstein's orchestrator reads the log and
  treats the run as successful.

the canned response shape mirrors the real claude-code stream so any
downstream consumer (parser, audit pipeline, telemetry) receives the
same event types it would in production.

## customising the canned response

```python
from custom_adapter import ClaudeMockAdapter

adapter = ClaudeMockAdapter(
    canned_responses={
        "fix the bug": "I patched src/foo.py.",
    }
)
```

bernstein instantiates the adapter without arguments by default; pass
custom canned responses by registering the adapter manually instead of
relying on entry-point discovery:

```python
from bernstein.adapters.registry import register_adapter
from custom_adapter import ClaudeMockAdapter

register_adapter("claude_mock", ClaudeMockAdapter(canned_responses={...}))
```

## test

```bash
cd examples/plugins/custom-adapter
pytest tests/ -q
```
