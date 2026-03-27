# Quickstart example

A minimal Flask TODO API that is intentionally rough around the edges:
no input validation, no error handling, no tests.

Run Bernstein against it and watch agents fix all three:

```bash
cd examples/quickstart
bernstein init
bernstein
```

Bernstein reads `bernstein.yaml`, plans the work, and spawns agents to add
validation, proper error responses, and a pytest suite.
