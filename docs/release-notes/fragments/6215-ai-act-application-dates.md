## `bernstein compliance assess` prints the AI Act date for the system's category

The report gave every high-risk system one deadline, "August 2027
(Article 111(2))". Article 111(2) is the rule for systems already on the
market. The application dates come from Article 113, and Regulation (EU)
2026/1744 (the Digital Omnibus on AI, OJ L, 2026/1744, 24.7.2026) replaced
them with separate dates for Annex III and Annex I systems. The Article 50
date was left unchanged, yet the report showed "N/A" for transparency-only
systems.

The dates now come from one table in `bernstein.compliance.eu_ai_act`. The
report, its next steps, and Annex IV section 8 print the date for each
obligation that applies, with its article: Article 50 transparency,
high-risk under Annex III, or high-risk under Annex I. The summary also
carries them as structured `deadlines` rows, with the transitional rules
beside them. `SystemDescriptor.annex_i_section_a_product` marks a system
that is high-risk under Article 6(1). A prohibited system no longer gets a
high-risk deadline (#6215).
