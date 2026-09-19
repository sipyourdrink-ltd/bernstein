## Query receipts now persist their signer's card on Windows

`QueryReceiptStore` wrote the signer's Agent Card at
`<root>/identity/<agent_id>/card.json`, using the raw agent id as a
directory component. Real agent ids in this codebase carry a colon
(`agent:datasource-1`), and `:` is not a legal path character on Windows,
so the first query receipt recorded on Windows raised
`NotADirectoryError` and the card was never persisted -- the receipt was
sealed and mirrored to the audit log but left permanently unverifiable.
The directory name is now a percent-encoded, collision-free, reversible
encoding of the agent id; the card's own `agent_id`/`kid` body fields
stay authoritative either way, matching the rule
`core.lineage.gate._load_cards` already states for its on-disk layout.
The card is also now persisted before the lineage entry is sealed, so a
card-write failure cannot leave an orphaned, unverifiable lineage entry
behind it (#5859).
