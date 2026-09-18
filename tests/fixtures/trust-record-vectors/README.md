# Trust Record test vectors

Seven TRACE v0.2 Trust Records, all produced by the real
`bernstein.core.observability.trust_record.TrustRecordEmitter` over real
`EventJournal`-recorded runs -- never hand-written JSON. See
`_build_trust_record_vectors.py` in this directory for exactly how each one
was built.

| File | What it is |
|---|---|
| `single-execution-trust-record.json` | one root (non-delegated) execution, with a tool call and a produced artifact |
| `delegated-parent-trust-record.json` | the parent hop of a two-hop delegated run |
| `delegated-child-trust-record.json` | the child hop, carrying `delegation` keyed on the parent's own record |
| `delegated-grandchild-trust-record.json` | a third hop under the child, so the chain is two links deep |
| `aggregate-trust-record.json` | the run-level rollup over parent+child+grandchild, carrying `references[rel=member-execution]` |
| `supplementary-plane-parent-trust-record.json` | a root hop whose `cnf.jwk` carries a key outside the Basic Multilingual Plane (see below) |
| `supplementary-plane-child-trust-record.json` | the child hop linked to it, whose `delegation.parent_record_hash` only resolves under RFC 8785 key order |
| `trust-record-vectors-key.pem` | the public half of the deterministic Ed25519 key that signed all seven |

## The supplementary-plane pair

RFC 8785 orders object property names as arrays of UTF-16 code units. The
shortcut most JSON libraries offer (`sort_keys=True`) orders them by Unicode
code point. The two agree on every ASCII and BMP-only name, so the five vectors
above are satisfied by a correct canonicalizer and by an incorrect one alike:
they show that two implementations agree, not what they agree about.

The parent of this pair carries two further RFC 7517 members in `cnf.jwk`, the
object whose digest the child asserts, with the same names and values as
trace-spec's `examples/delegation-link/24-parent-key-supplementary-plane.json`:

| Member | Code point | UTF-16 code units | JCS order | code-point order |
|---|---|---|---|---|
| `"\ue000": "bmp-private-use"` | U+E000 | `E000` | second | first |
| `"\U0001f600": "supplementary-plane"` | U+1F600 | `D83D DE00` | first | second |

`D83D < E000`, so RFC 8785 puts the supplementary-plane member first and a
code-point sort puts it second. Two consequences, both asserted in
`tests/unit/test_trust_record_format_vectors.py`:

- the child's `delegation.parent_record_hash` equals the parent's digest under
  RFC 8785 (trace-spec section 3.1.3) and differs from its digest under a
  code-point sort, so a verifier taking the shortcut reports `parent_not_found`
  on a chain that is otherwise the ASCII pair's;
- the parent's `signature` verifies over its RFC 8785 pre-image and fails over
  the code-point one, so the same shortcut cannot verify the parent either.

The committed file itself is `json.dumps(sort_keys=True)` output with ASCII
escapes, which is code-point order: the file is a JSON document, and the record
is what parsing it yields. Digest the parsed record under RFC 8785, never the
file's bytes.

The members reach the record through the emitter's `cnf_jwk_members`
parameter and the production signing path (`sign_trust_record` over
`canonicalize_jcs`); no schema was changed, since `cnf.jwk` already admits
further members.

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
