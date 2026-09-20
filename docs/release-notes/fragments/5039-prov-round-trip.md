## PROV-O export distinguishes rotated agent keys

`bernstein lineage export-prov` used to key each operator `prov:Agent` by
`agent_id` alone, so two entries signed by the same agent under different
key ids collapsed into one node and the export lost the agent identity the
lineage gate verifies (`agent_id`, `agent_card_kid`). The agent URI now
embeds the card key id, and a round-trip test pins that the node and edge
set survives graph to PROV-JSON and back unchanged. (#5039)
