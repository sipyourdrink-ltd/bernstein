# Authority-envelope golden vectors

Committed test vectors for `schemas/authority-envelope-v1.json` and the
standalone verifier in `verify_cli/bernstein_verify_envelope/`.

| File | What it is |
|---|---|
| `valid-authority-envelope.json` | A deliberately **partial** envelope: two authorization decisions, one of which carries no evidence and is therefore named in `coverage.uncovered`. Verifies clean. |
| `tampered-authority-envelope.json` | The same envelope with `decisions[1].verdict` flipped to `deny`. Must be rejected, and the rejection must name the `decisions` section. |
| `_build_authority_envelope_vectors.py` | Re-mints both files. Run by hand, never by the test suite. |

## Why the vectors are committed

The vectors are the target the verifier is judged against. Minting them inside
the test would move both sides of the comparison at once: a verifier that
stopped checking `inputs_hash` and a builder that stopped computing it would
still agree with each other. Because the bytes are committed, a change to the
canonical form, a hash preimage, or the JWS signing input fails CI instead of
silently invalidating an envelope already handed to an auditor.

The builder is fully deterministic — fixed timestamps, seeded Ed25519 keys — so
re-minting an unchanged format reproduces the files byte for byte. Re-mint only
when the envelope format itself changed:

```bash
uv run python tests/fixtures/authority-envelope-vectors/_build_authority_envelope_vectors.py
```

Review the resulting diff as new evidence rather than as a formatting change.

## Keys

Both keys are seeded from constants in the builder and exist only for these
vectors. They are not operator keys and must never be trusted for anything else.

- Principal key: seed `b"p" * 32`.
- Attestor (signing) key: seed `b"a" * 32`. The envelope carries its public half
  as a hint; a verifier is expected to pin its own key out of band.
