## A foreign attestation can no longer claim Bernstein's own trust class

The foreign attestation verifier took an attestation's `trust_class` from the
attestation itself. A claim declaring `operator`, `workspace` or `first_party`
trust kept that class as its taint, so `provenance.is_untrusted` treated it as
untainted; with any key that had signed the envelope's payload hash,
`verify_foreign_attestation_full` also reported it `verified_foreign`.

A foreign attestation is material Bernstein did not issue, so its trust class
is now capped at the outsider classes. A claim above `third_party` is
`malformed` and fails closed at `public`, signed or not. `third_party` and
`public` claims verify and classify exactly as before (#3133).
