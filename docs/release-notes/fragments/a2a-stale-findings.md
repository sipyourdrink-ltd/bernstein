## Three A2A bughunt findings were fixed but still recorded as open

`tests/property/test_a2a_card_bughunt.py` keeps an index of findings against the signed agent-card surface, each pinned by an xfail. Three of them describe security controls that have since shipped, and the tests holding them open could not have detected that:

- **JWKS rotation grace window.** `agent_json_keys` appends every archived public key still inside the keystore's grace window, which is what RFC 7517 expects of a rotation. The test simulated rotation by resetting the in-process cache twice, which predates persistence: resetting now reloads the same key from disk, so it asserted against a rotation that never happened.
- **Private signing key file mode.** `AgentCardKeystore` creates the private PEM with `O_EXCL` and mode `0600`, chmods it again after write, and refuses to load a key already on disk with looser permissions. The test asserted a path the implementation never used, so it failed on "no persisted key file" and reported the control missing.
- **RFC 8707 resource indicators.** `auth_middleware` consults the JWT `resource` claim and answers a mismatch with the RFC 6750 challenge. The test body raised unconditionally with "not implemented".

All three now have tests that exercise the shipped control, including the negatives that make them worth having: a key past the grace window stops being advertised, a key loosened after write is refused rather than loaded, and enforcement stays opt-in when no resource is configured.

Finding #10, the unbounded rotation archive, is genuinely still open and its entry now says why: rotation moves the previous keypair under `archive/`, `list_archived` filters that by the grace window when publishing, and nothing removes an entry once it falls outside.
