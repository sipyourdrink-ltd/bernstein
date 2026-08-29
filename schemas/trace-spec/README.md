# Vendored TRACE schema

JSON Schema for the TRACE Trust Record format, vendored verbatim so trust-record
validation never fetches anything at load time (air-gap deploys validate offline
against these exact bytes).

| File | Validates |
|---|---|
| `0.2/trace-v0.2.json` | a TRACE v0.2 Trust Record (execution or aggregate) |

- Source: https://github.com/agentrust-io/trace-spec (`src/agentrust_trace/schema/trace-v0.2.json`)
- Spec version: 0.2
- Retrieved: 2026-08-29 (upstream commit `e7e2ecab68cf3534c7d5fcb7e9a6f089fcb7d592`)

Do not hand-edit this file. To move to a newer spec version, vendor it as a new
`schemas/trace-spec/<version>/` directory and update the generator and tests
to pin it explicitly.

## Conformance harness (issue #4764)

`tests/unit/test_trust_record_conformance.py` validates every committed vector
under `tests/fixtures/trust-record-vectors/` against this schema with
`jsonschema`, round-trips each through `bernstein.core.observability.trust_record`'s
`sign_trust_record`/`verify_trust_record` pair, and (when the optional
`agentrust-trace-tests` conformance CLI is resolvable) additionally runs the
upstream executable suite:

```
uv run --with agentrust-trace-tests==0.5.1 trace-tests verify \
    --record tests/fixtures/trust-record-vectors/<name>.json \
    --level 0 --max-age 999999999999
```

(`--max-age` set high because the fixture vectors are signed under a frozen
2023-11-14 clock, not wall-clock time -- see
`tests/fixtures/trust-record-vectors/_build_trust_record_vectors.py`.) Result at
the time of writing: `PASS (8 checks, 0 skipped)` for all four vectors. The test
resolving this CLI needs network access to fetch it from PyPI (it is not a
project dependency -- see the test module docstring for why); it is skipped,
not failed, when that resolution is unavailable.
