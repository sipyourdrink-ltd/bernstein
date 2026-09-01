# Trust Record test vectors

Four TRACE v0.2 Trust Records, all produced by the real
`bernstein.core.observability.trust_record.TrustRecordEmitter` over real
`EventJournal`-recorded runs -- never hand-written JSON. See
`_build_trust_record_vectors.py` in this directory for exactly how each one
was built.

| File | What it is |
|---|---|
| `single-execution-trust-record.json` | one root (non-delegated) execution, with a tool call and a produced artifact |
| `delegated-parent-trust-record.json` | the parent hop of a two-hop delegated run |
| `delegated-child-trust-record.json` | the child hop, carrying `delegation` keyed on the parent's own record |
| `aggregate-trust-record.json` | the run-level rollup over the parent+child pair, carrying `references[rel=member-execution]` |
| `trust-record-vectors-key.pem` | the public half of the deterministic Ed25519 key that signed all four |

## Upstream pin

- Spec: https://github.com/agentrust-io/trace-spec
- Commit: `e7e2ecab68cf3534c7d5fcb7e9a6f089fcb7d592`
- Vendored schema this repo validates against: `schemas/trace-spec/0.2/trace-v0.2.json`
  (see `schemas/trace-spec/README.md`)

## Regenerating

Vectors are **never hand-edited**. To re-mint them after a change to the
emitter or the generator:

```
uv run python tests/fixtures/trust-record-vectors/_build_trust_record_vectors.py
```

The generator freezes the journal clock and every other timestamp source, so
running it twice must produce byte-identical output --
`tests/unit/test_trust_record_format_vectors.py::test_regenerating_the_vectors_is_byte_identical_to_the_committed_files`
enforces this. Re-mint only when the Trust Record format itself changed, and
review the diff as new evidence: it cannot tell you which part of it moved.

## Verifying

Three independent checks, all exercised in CI:

1. **Own signature, offline** --
   `tests/unit/test_trust_record_format_vectors.py` and
   `tests/unit/core/observability/test_trust_record.py` re-verify each
   vector's `signature` against its own `cnf.jwk`, with no external tool.
2. **Vendored schema** -- `tests/unit/test_trust_record_conformance.py`
   validates every vector against `schemas/trace-spec/0.2/trace-v0.2.json`
   with `jsonschema`, and round-trips each through this repo's own
   `sign_trust_record`/`verify_trust_record` pair
   (`bernstein.core.observability.trust_record`).
3. **Reference executable conformance suite** -- the same test module
   resolves `agentrust-trace-tests` on demand (`uv run --with
   agentrust-trace-tests==0.5.1 trace-tests verify --record <path> --level 0
   --max-age 999999999999`) and runs it against every vector, skipping
   (not failing) when that resolution needs network access that is not
   available. See `schemas/trace-spec/README.md` for the last known-good
   result.

To run the reference suite by hand against any one vector:

```
uv run --with agentrust-trace-tests==0.5.1 trace-tests verify \
    --record tests/fixtures/trust-record-vectors/single-execution-trust-record.json \
    --level 0 --max-age 999999999999
```

(`--max-age` is set far above the default 24h window because every vector's
`iat` is sourced from the generator's frozen 2023-11-14 fixture clock, not
wall-clock time -- an unmodified default would reject all four as stale.)
