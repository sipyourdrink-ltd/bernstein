# bernstein-verify-envelope

Standalone verifier for a **Bernstein authority envelope**: a single file that
records who acted, under which delegated authority, which policy decision
allowed it, what evidence ties the decision to an artefact, and — stated in the
file itself — what the envelope does *not* cover.

The point of the envelope is that it means something outside the install that
produced it. This verifier is the proof: it imports only `cryptography` and
`click`, never `bernstein`, and never opens a socket.

```bash
pip install bernstein-verify-envelope

# Checked against the key the envelope carries: reported as trust-on-first-use.
bernstein-verify-envelope verify ./authority-envelope.json --verbose

# Checked against a key you obtained out of band: an envelope re-signed by
# anyone else is rejected.
bernstein-verify-envelope verify ./authority-envelope.json --jwk ./operator.jwk
bernstein-verify-envelope verify ./authority-envelope.json --public-key ./operator.pem
```

Exit codes: `0` verified, `1` a check failed, `2` bad arguments. Pass at most one
of `--jwk` / `--public-key`: two pins are refused rather than one being ignored.

Every run ends with a `TRUST:` line and the machine-readable result on stderr
carries a `trust` member, so a pass against a pinned key is never confused with
a pass against the key the file supplies:

| `trust` | What the pass means |
|---|---|
| `pinned-jwk` / `pinned-public-key` | The signature verifies under a key supplied out of band. |
| `trust-on-first-use` | Nothing was edited after signing. It is *not* evidence the signer is who the envelope names. |

## What it checks

| Check | What it proves |
|---|---|
| `envelope_type` | The file declares the schema version and type this verifier implements. |
| `coverage` (gate) | The envelope states what it does not cover. An envelope with no `coverage` section is refused outright — silence is not read as full coverage. |
| `signature` | The detached EdDSA JWS verifies over the RFC 8785 canonical bytes of the envelope body, under the pinned key when one is given and otherwise under the key the envelope carries. |
| `section:<name>` | Each section's recorded digest recomputes, so a failure names the section that moved rather than only reporting a broken signature. |
| `principal` | The principal identifier is bound to its key material by a recomputed hash. |
| `grants` | The chain hashes recompute and every link attenuates: subset scope, no later expiry, issuer equal to the previous subject, terminating at the principal. |
| `decisions` | Each decision's input hash recomputes from its own recorded inputs and the grant it cites; an `allow` outside that grant's scope, or a decision taken after the grant expired, is rejected. |
| `evidence` | Every artefact hash attaches to a decision the envelope carries. |
| `coverage` | The coverage statement recomputes: the decisions carrying evidence and the gaps are both re-derived from the envelope, so a hidden gap fails. |

## What a passing envelope does *not* prove

- **That the key is trusted, unless you pinned one.** Verifying against the key
  the envelope carries is trust-on-first-use, and is reported as such: an
  attacker who can replace the whole file can also replace the key inside it.
  Pin the signer's key with `--jwk` or `--public-key` and that stops being true
  — but where the pinned key comes from is still your problem, not the
  envelope's.
- **That the grants were unrevoked** at the time they were used. The envelope
  carries expiries, not revocation state.
- **That the artefacts exist.** Evidence entries are hashes; matching them to
  real artefacts is the auditor's step.
- **Anything outside its `coverage`.** A partial envelope names its own gaps and
  the verifier enforces that they are named — but the gaps remain gaps.

## Canonical form

RFC 8785 JCS, re-implemented in `verify.py` rather than imported, so agreement
with the producer is demonstrated by the committed golden vector instead of
assumed by sharing code. The signature is a detached compact JWS (RFC 7515
Appendix F) whose signing input is
`BASE64URL(JCS(header)) || "." || BASE64URL(JCS(body))`, where the body is every
top-level member except `signature`.

The schema is `schemas/authority-envelope-v1.json`; the vectors are under
`tests/fixtures/authority-envelope-vectors/`.
