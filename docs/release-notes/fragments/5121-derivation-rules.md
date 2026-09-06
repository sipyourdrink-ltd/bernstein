## Derived classification facts come from declared rules that name themselves

Site, owning team, contact and per-target endpoints were facts nothing
derived. `bernstein.core.govern.derivation` makes them data: a rule file
declares `network prefix -> site`, `owner -> contact` and
`site -> endpoints`, and every fact records the `rule_id` that produced
it, so a wrong answer is debuggable by reading one field.

Rules are validated at load, not at use — an unknown key, an empty field,
an unfamiliar kind, a prefix that is not a valid network, or two rules of
one kind claiming the same match are all refused with the reason. A
malformed rule silently producing wrong ownership is worse than one that
fails to load.

**Ownership cannot be derived from a name pattern.** `team-*`,
`host[0-9]`, `regex:` and friends are refused on an `owner_contact` rule:
it is wrong the first time a host is named after the wrong team, and
wrong silently. The most specific prefix wins, not the first declared,
and a target no rule matches derives `unknown` — a value, so it shows up
in a gap query (#5121).
