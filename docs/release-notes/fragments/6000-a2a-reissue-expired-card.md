## `a2a publish` recovers from an expired card on its own

`bernstein a2a publish` reuses the capability card persisted at
`.sdd/a2a/published-card.json`, and that card expires after 24 hours. Once it
did, the loader handed the expired card to the publish gate, which refused with
"refusing to publish an expired capability card" — and kept refusing on every
later run, because nothing re-issued the card. A node that published once and
came back the next day could not publish again until someone deleted the card
file by hand.

The loader now re-issues the card on the same persisted key when the stored one
is past its `expires_at`, so the node's identity (its key fingerprint) survives
across validity windows and publishing works again without manual cleanup. An
unexpired card is still reused as-is (#6000).
The re-issue carries the card's own claims (issuer, name, tools, policies)
onto the key persisted beside it, so an operator-authored card keeps what it
asserts across validity windows. The stored signature is verified before
anything is re-signed, so a card whose body was edited on disk is refused
rather than re-signed under the node's key. If that key file is missing or
belongs to a different card, publish refuses instead of silently minting a
new identity — restore the key the card was issued with, or mint a new card
with `bernstein interop a2a card` and re-trust the new fingerprint.
