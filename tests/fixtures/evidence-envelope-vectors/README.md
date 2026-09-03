# Evidence-envelope test vectors

One golden evidence envelope (v1) plus the public half of the deterministic
Ed25519 key that signed it.

| File | What it is |
|---|---|
| `partial-coverage-envelope.json` | an envelope over five declared actions, three of them covered; the other two are named in `coverage.uncovered` |
| `partial-coverage-envelope.sha256` | the SHA-256 of those exact bytes, in `sha256sum` format |
| `evidence-envelope-vectors-key.pem` | the public key the envelope's detached JWS verifies against |
| `_build_evidence_envelope_vectors.py` | the generator; the vector is never hand-edited |

The file's bytes *are* its canonical form: JCS (RFC 8785), one line, no
trailing newline. Re-encoding the parsed file with
`bernstein.core.security.evidence_envelope.canonical_envelope_bytes` must
reproduce it byte for byte, which is what
`partial-coverage-envelope.sha256` is a checkable statement about.

Verify the digest by hand from this directory:

```
shasum -a 256 -c partial-coverage-envelope.sha256
```

## What the vector is, and is not

The envelope's *content* is authored: this slice ships no producer, so
there is nothing to project a real run from. It is a worked example chosen
to exercise the one rule the format exists for -- an envelope that covers
part of its scope and says which part it does not.

The *encoding* is not authored. The canonical bytes and the signature both
come from production functions, so the committed file can detect encoding
drift: `tests/unit/test_evidence_envelope_format_vectors.py` re-encodes it
with today's canonicaliser and re-verifies the committed signature offline.
A change to either side diverges from a file that was already published.

## Regenerating

```
uv run python tests/fixtures/evidence-envelope-vectors/_build_evidence_envelope_vectors.py
```

Every input is a constant -- pinned signing seed, fixed timestamps -- so
running it twice produces byte-identical output;
`test_regenerating_the_vector_is_byte_identical_to_the_committed_file`
enforces that by calling the generator against a temporary directory.

Re-mint only when the envelope format itself changed, and review the diff as
new evidence: it cannot tell you which part of it moved.

The signing key is a test key, published here alongside the vector it signs.
It is not an installation identity and must never become one.
