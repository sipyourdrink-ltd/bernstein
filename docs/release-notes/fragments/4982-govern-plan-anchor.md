## The govern plan artefact names its own decision record

`bernstein govern plan` records the plan in the lineage journal and then writes
it to disk, but the file it wrote dropped the anchor it had just been recorded
under. The written plan now carries the `journal_entry_hash` of that entry, and
the hash is excluded from the anchored bytes so the recorded content hash still
matches the file. A plan handed back for execution can be checked against the
journal entry that captured it instead of being taken on trust.

(#4982)
