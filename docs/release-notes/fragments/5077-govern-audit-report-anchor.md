## The govern audit report is a chain-anchored artefact

A governance audit report is now an artefact rather than a printout. Its canonical bytes are a deterministic function of the findings it carries, so its sha256 identifies a posture: two audits over an unchanged install produce byte-identical reports. That identity is anchored in the lineage spine over exactly those bytes, the way a governance decision is, and `bernstein audit verify` gained a `Govern Audit Reports` pillar that re-derives the anchor offline — an edited stored report matches no spine entry and fails verification instead of reading as a different posture. A report also records the anchor of the inventory it audited, so "audited what was enumerated on that date" is one chain walk, and `diff_reports` computes drift between two stored reports — only findings whose verdict or evidence hash changed — without re-running any check.

(#5077)
