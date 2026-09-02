# Authority envelope

An **authority envelope** is a single file that records who acted, under which
delegated authority, which policy decision allowed it, what evidence ties that
decision to an artefact, and — stated in the file itself — what the envelope
does *not* cover.

It exists so that evidence about authority can be checked somewhere other than
the install that produced it: by a gateway, another agent runtime, a policy
engine, or an auditor's own tooling.

Two artefacts define it today:

| Artefact | Path |
|---|---|
| Schema (v1) | `schemas/authority-envelope-v1.json` |
| Standalone verifier | `verify_cli/bernstein_verify_envelope/` |
| Golden vectors | `tests/fixtures/authority-envelope-vectors/` |
| Run-scoped producer | `src/bernstein/core/interop/authority_envelope.py` |

## Producing one from a run

`build_run_authority_envelope` reads the `GovernanceDecision` records persisted
under `lineage_root/<run_id>/` and renders the subset concerning one principal:

```python
from bernstein.core.interop.authority_envelope import build_run_authority_envelope

envelope = build_run_authority_envelope(
    lineage_root=Path(".sdd/lineage"),
    run_id="run-2026-09-02-a",
    principal_id="urn:bernstein:principal:agent:reviewer-7",
    principal_public_key_pem=principal_public_pem,
    idp_groups=("eng-operators",),
    bindings=signed_role_bindings,
    grant_id="grant-role-operator",
    grant_issuer="urn:bernstein:principal:operator:alex",
    grant_not_after="2031-01-01T00:00:00Z",
    signing_key_pem=signing_pem,
    signing_kid="envelope-signer-1",
)
```

The authority the envelope records is the one the run resolved: the principal's
IDP groups map to a role through the signed `RoleBindings`, and that role's
permission set is the scope of the single grant link. The schema admits a
multi-link chain; nothing writes delegation hops yet, so the producer emits one
link.

Every field is a pure function of the records on disk and the arguments above —
no clock, no ordering by build time — so two builds over the same run are
byte-identical and an envelope can be re-derived and diffed rather than trusted.

### What the producer refuses to emit

- A record claiming `allow` for an action outside the resolved role's permission
  set. Signing it would assert authority the bindings never granted, so the
  build raises `AuthorityEnvelopeError`.
- A record timestamped after the grant expires.

### What it leaves out, and says so

`coverage` is computed from the records, never asserted:

- Records about **other subjects** are not carried; `coverage.statement` gives
  the count.
- **Budget records** (`check_budget_decision`) draw authority from the spend
  policy rather than from the role grant, so they are not carried under a role
  grant either; the statement gives that count too.
- A carried decision whose record has **no lineage-spine anchor** has no
  evidence, and is listed in `coverage.uncovered` with the reason.

Evidence entries are the decision records' lineage anchors, so a reader with the
run's spine can match each decision to the entry that recorded it.

## Verifying one

```bash
pip install bernstein-verify-envelope
bernstein-verify-envelope verify ./authority-envelope.json --verbose

# With a trust source obtained out of band (at most one of the two):
bernstein-verify-envelope verify ./authority-envelope.json --jwk ./operator.jwk
bernstein-verify-envelope verify ./authority-envelope.json --public-key ./operator.pem
```

Exit codes: `0` verified, `1` a check failed, `2` bad arguments. The verifier
depends on `cryptography` and `click` and nothing else — in particular it never
imports `bernstein` and never opens a socket, which is asserted by a test that
runs it in a subprocess where importing `bernstein` raises.

## Shape

```
schema_version    pinned to 1.0.0
envelope_type     https://bernstein.run/attestations/authority-envelope/v1
principal         the acting identity, plus the key material to check it
grants            the authority it acted under, root first, each link attenuating
decisions         one record per authorization, each naming a versioned policy
evidence          artefact hashes tying decisions to what happened
coverage          what this envelope does NOT cover, stated in the envelope
section_digests   per-section hashes, so a failure names the section that moved
signature         detached EdDSA JWS over the canonical bytes of everything above
```

Nothing in the envelope references a run identifier or a lineage root, so it can
carry activity Bernstein did not schedule.

### Canonical form

RFC 8785 JCS. The signature is a detached compact JWS (RFC 7515 Appendix F)
whose signing input is `BASE64URL(JCS(header)) || "." || BASE64URL(JCS(body))`,
where the body is every top-level member except `signature`, and the protected
header is `{"alg": "EdDSA", "typ": "application/vnd.bernstein.authority-envelope+jws", "kid": …}`.

The verifier re-implements JCS locally rather than importing it. The committed
golden vector is what proves the two implementations agree; sharing the code
would prove nothing about an independent reader.

## What a passing envelope proves

- **The identifier is bound to the key.** `principal.id_binding` recomputes over
  the principal id and its JWK, so an identifier cannot be re-pointed at other
  key material.
- **The grant chain attenuates.** Each link's hash chains to its parent's, its
  scope is a subset of the parent's, its expiry is no later, its issuer is the
  parent's subject, and the last link's subject is the acting principal.
  Rewriting an ancestor invalidates every descendant.
- **Each decision follows from the authority it cites.** The decision's
  `inputs_hash` recomputes from its own recorded policy inputs *and* the grant
  hash, so an edited input or a swapped grant is detected. An `allow` for an
  action outside the cited link's scope is rejected, as is a decision timestamped
  after that link expired.
- **Evidence attaches to something.** Every artefact hash names a decision the
  envelope carries.
- **The gaps are declared.** The verifier re-derives which decisions carry
  evidence and which do not, and rejects a `coverage` section that does not name
  every gap. An envelope with no `coverage` section at all is refused rather than
  read as complete.
- **Nothing was edited after signing.** The detached JWS covers the whole body,
  and the per-section digests localise a mutation to the section it landed in.

## What it does not prove

- **That the signing key is trusted, when no key is pinned.** An envelope
  verified against the key it carries is trust-on-first-use, and is reported as
  such: an attacker who can replace the whole file can also replace the key it
  carries. Pass `--jwk` or `--public-key` to verify against a key obtained out of
  band; an envelope re-signed by any other key is then rejected. Where that key
  comes from remains out of scope for the envelope.
- **That the grants were unrevoked** when they were used. The envelope carries
  expiries, not revocation state.
- **That the artefacts exist or say what they are claimed to say.** Evidence
  entries are hashes; matching them against real artefacts is the reader's step.
- **That the acting principal was who it claimed to be.** The envelope binds an
  identifier to a key; it does not prove possession of that key at the time of
  the action.
- **Anything outside its own `coverage`.** A partial envelope names its gaps and
  the verifier enforces that they are named — the gaps remain gaps.

## Golden vectors

`tests/fixtures/authority-envelope-vectors/` holds a deliberately partial
envelope (two decisions, one of them uncovered) and a tampered copy. Both are
committed rather than minted at test time, so a change to the canonical form or
a hash preimage fails CI instead of silently invalidating an envelope already
handed to a reader. The builder is deterministic; re-mint it only when the
format itself changes:

```bash
uv run python tests/fixtures/authority-envelope-vectors/_build_authority_envelope_vectors.py
```
