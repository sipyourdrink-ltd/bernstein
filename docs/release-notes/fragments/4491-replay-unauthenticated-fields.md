## Replay names the fields its verdict does not cover

Journal payload hashing excludes the wall-clock envelope by design, so `ts`,
`elapsed_s` and the derived chain fields on stored rows are not covered by a
chain-verification pass -- but nothing in the verified output said so, and a
reader naturally assumed the whole row was attested. `replay --verify` output
and the portable receipt manifest now name the unauthenticated field set,
machine-readably in structured output, and the field-coverage contract is
documented in one table (#4209).
