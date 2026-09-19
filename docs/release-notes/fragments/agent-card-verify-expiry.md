## An expired agent card no longer verifies

`verify_agent_card` checked the detached JWS and nothing else. A signature stays valid for as long as its key does; the card underneath it does not, so an expired card was byte-for-byte indistinguishable from a current one to the function whose name invites a caller to trust it. Anyone who captured a card once could replay it indefinitely.

The verifier now composes the card's validity window - both ends of it, so a card is refused before `created_at` as well as at or after `expires_at` - and `AgentIdentityCard.is_valid_at` states that rule once for every caller. `at_time` selects the instant to judge, because `IdentitySpawnAnchor.reconstruct` replays an attestation recorded under a card that has since expired and must keep verifying; live callers get "now" by default (#4810).
