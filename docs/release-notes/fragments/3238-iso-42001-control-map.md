## ISO/IEC 42001 Annex A controls in an evidence pack

`bernstein audit export --standard` accepts `iso-42001`, mapping the
records-derivable subset of ISO/IEC 42001 Annex A onto the same audit chain,
lineage log and cost ledger the `ai-act`, `owasp-asi` and `owasp-skills` packs
already read: event logging, operation monitoring, data provenance, third-party
and data resources, and human oversight of individual decisions. A control
resolves to `mapped`, `partial`, or `organisational` — the last naming a control
no chain record can evidence, rather than overclaiming it as covered. The pack
summary counts `organisational` controls alongside the other states, so no
control in the map is dropped from the totals. `docs/compliance/iso42001-mapping.md`
lists every mapped control and its state.

(#3238)
