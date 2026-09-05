## Two A2A findings were fixed but still recorded as open, and two were not

`tests/property/test_a2a_card_bughunt.py` keeps an index of findings against the signed agent-card surface, each pinned by an xfail. Three were checked against what has since shipped.

**Finding #7, the private signing key file mode, is closed.** `AgentCardKeystore` creates the private PEM with `O_EXCL` and mode `0600`, chmods it again after write, re-chmods on archive, and refuses to load a key already on disk with looser permissions. The test asserted a path the implementation never used, so it failed on "no persisted key file" and reported the control missing.

**Finding #6, the JWKS rotation grace window, is half closed.** `agent_json_keys` now publishes every archived key still inside the window, so a verifier that tries every key is rescued. A verifier that routes by `kid` is not: a card is signed under the stable kid while an archived key is published under a timestamped one, so after a rotation the stable kid resolves to the new key and the retired key sits under a kid no card ever referenced. That half is now pinned as its own xfail.

**Finding #8, RFC 8707 resource indicators, is implemented but not closed.** The check and its RFC 6750 challenge are real, but `expected_resource` defaults to empty so enforcement is off on a stock install, and where it is configured a token carrying no `resource` claim passes anyway. Both gaps are named in the record rather than left implied by a finding marked fixed.

Finding #10, the unbounded archive of retired private keys, remains open and unchanged (#5512).
