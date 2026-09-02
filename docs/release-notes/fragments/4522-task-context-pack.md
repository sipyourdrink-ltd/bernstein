## Repository evidence for a task spawn

`BERNSTEIN_TASK_CONTEXT_PACK`, off unless set, assembles what this repository
already records about the files a task owns — co-change neighbours and the
test-to-source map from the commit graph, the nearest `AGENTS.md` verbatim, and
the tests the gate has quarantined — into one named prompt section for the
spawn. The derivations existed but nothing called them, so none of that evidence
reached an agent. The pack comes only from the commit graph and the tree with no
model in the assembly path, and targets are normalised to a sorted set and
sections to a fixed order before serialisation, so the same repository state
yields the same bytes and therefore the same content address; because it travels
as one section, the per-section hash the context receipt already records is the
run record for the pack that spawn consumed, re-derivable and re-checkable
offline. The per-list cap and the byte budget each record what they cut inside
the pack, since a silently shortened list reads as "there was nothing else".
Each source fails open on its own — an unreadable one names itself and the rest
still contribute — and an empty pack appends no section at all, leaving the
spawn prompt byte-identical to what it is with the flag off.

(#4522)
