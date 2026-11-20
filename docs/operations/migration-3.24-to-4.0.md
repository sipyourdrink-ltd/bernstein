# Migrating from 3.24 to 4.0

## Quality-gate seed parsing

Previously omitted supported `quality_gates` values now reach
`QualityGatesConfig` through [`_parse_quality_gates`][gate-parser]. These include
`mutation_testing`, `dep_audit`, `intent_verification`, `large_file_check`,
`migration_reversibility_check`, `test_expansion`, `agent_test_mutation`,
`integration_test_gen`, `review_rubric`, and their supported options. Before this
parser fix, those seed settings left the gates at their defaults, as described
in the [release note][gate-note].

Upgrade checklist:

- Review gate values already present in your seed: they can now affect the
  configuration. In particular, the repository's [`bernstein.yaml`][seed] sets
  `large_file_check: true`, and that value now takes effect
  ([release note][gate-note], [parser][gate-parser]).
- Supply `quality_gates.intent_verification` as a mapping, rather than a scalar
  such as `true`. A non-mapping value raises `SeedError`; an omitted or null
  value uses `IntentVerificationConfig()`
  ([`_parse_quality_gate_intent`][intent-parser]).
- Use integers for integer fields. Boolean values such as `timeout_s: true` or
  `intent_verification.max_tokens: true` raise `SeedError`, even though Python
  treats `bool` as a subclass of `int`
  ([gate parser][gate-parser], [intent parser][intent-parser]).

**Compatibility limit:** this is a change in parsing supported seed options,
not a version-triggered change to gate defaults. Omitted values retain the
defaults in [`QualityGatesConfig` and `IntentVerificationConfig`][gate-config];
for example, `large_file_check` still defaults to `false`. Explicit seed values
override those defaults through the [parser][gate-parser].

[gate-parser]: https://github.com/sipyourdrink-ltd/bernstein/blob/19672b2645438131a2a23b8992803ad5ef94b71e/src/bernstein/core/config/seed_parser.py#L1841-L1999
[intent-parser]: https://github.com/sipyourdrink-ltd/bernstein/blob/19672b2645438131a2a23b8992803ad5ef94b71e/src/bernstein/core/config/seed_parser.py#L2047-L2095
[gate-config]: https://github.com/sipyourdrink-ltd/bernstein/blob/19672b2645438131a2a23b8992803ad5ef94b71e/src/bernstein/core/quality/quality_gates.py#L86-L294
[gate-note]: https://github.com/sipyourdrink-ltd/bernstein/blob/19672b2645438131a2a23b8992803ad5ef94b71e/docs/release-notes/fragments/6615-quality-gate-keys-from-seed.md
[seed]: https://github.com/sipyourdrink-ltd/bernstein/blob/19672b2645438131a2a23b8992803ad5ef94b71e/bernstein.yaml#L33-L42
