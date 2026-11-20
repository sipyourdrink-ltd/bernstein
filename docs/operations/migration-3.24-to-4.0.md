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

## Workflow failure boundaries

When a node fails, the [`WorkflowRunner.run` execution loop][workflow-run] sets
the run's abort state. Every later layer is skipped, including nodes whose own
dependencies succeeded. Those layers do not dispatch commands or agents, and
each skipped node is appended to the execution result once. This aligns `run`
with the existing `resume` failure boundary
([release note][workflow-note], [`resume` loop][workflow-resume]).

Before/after check: suppose `a` and `b` share a layer and `c` has
`depends_on: [b]`. If `a` fails and `b` succeeds, the earlier behavior could
still execute `c` and include it twice in the result; now `c` is recorded once
as `SKIPPED` without executing. Review workflows that expected a successful
branch to keep later layers running after another branch failed
([manifest vocabulary and layer ordering][workflow-spec],
[release note][workflow-note], [execution loop][workflow-run]).

An intentional condition skip has different semantics: a false `when`
predicate produces `NodeExecution(status=SKIPPED, condition_skipped=True)`.
Such a skip alone does not fail the run and satisfies a downstream dependency.
The success reduction requires no abort and every node to be successful or
intentionally condition-skipped; a skip caused by a failed dependency does
not qualify ([`NodeExecution`][workflow-result],
[`when` evaluation][workflow-when], [dependency check][workflow-dependency],
[success reduction][workflow-run]).

**Compatibility limit:** the abort boundary stops later layers. It does not
cancel work already dispatched in the current layer: the runner collects that
layer's results before advancing. Do not rely on this boundary to interrupt
another node already running in the same layer
([execution loop][workflow-run], [`_execute_layer`][workflow-layer]).

[gate-parser]: https://github.com/sipyourdrink-ltd/bernstein/blob/19672b2645438131a2a23b8992803ad5ef94b71e/src/bernstein/core/config/seed_parser.py#L1841-L1999
[intent-parser]: https://github.com/sipyourdrink-ltd/bernstein/blob/19672b2645438131a2a23b8992803ad5ef94b71e/src/bernstein/core/config/seed_parser.py#L2047-L2095
[gate-config]: https://github.com/sipyourdrink-ltd/bernstein/blob/19672b2645438131a2a23b8992803ad5ef94b71e/src/bernstein/core/quality/quality_gates.py#L86-L294
[gate-note]: https://github.com/sipyourdrink-ltd/bernstein/blob/19672b2645438131a2a23b8992803ad5ef94b71e/docs/release-notes/fragments/6615-quality-gate-keys-from-seed.md
[seed]: https://github.com/sipyourdrink-ltd/bernstein/blob/19672b2645438131a2a23b8992803ad5ef94b71e/bernstein.yaml#L33-L42
[workflow-run]: https://github.com/sipyourdrink-ltd/bernstein/blob/19672b2645438131a2a23b8992803ad5ef94b71e/src/bernstein/core/workflows/workflow_runner.py#L623-L710
[workflow-resume]: https://github.com/sipyourdrink-ltd/bernstein/blob/19672b2645438131a2a23b8992803ad5ef94b71e/src/bernstein/core/workflows/workflow_runner.py#L461-L535
[workflow-result]: https://github.com/sipyourdrink-ltd/bernstein/blob/19672b2645438131a2a23b8992803ad5ef94b71e/src/bernstein/core/workflows/workflow_runner.py#L97-L154
[workflow-when]: https://github.com/sipyourdrink-ltd/bernstein/blob/19672b2645438131a2a23b8992803ad5ef94b71e/src/bernstein/core/workflows/workflow_runner.py#L853-L871
[workflow-dependency]: https://github.com/sipyourdrink-ltd/bernstein/blob/19672b2645438131a2a23b8992803ad5ef94b71e/src/bernstein/core/workflows/workflow_runner.py#L714-L726
[workflow-layer]: https://github.com/sipyourdrink-ltd/bernstein/blob/19672b2645438131a2a23b8992803ad5ef94b71e/src/bernstein/core/workflows/workflow_runner.py#L728-L761
[workflow-spec]: https://github.com/sipyourdrink-ltd/bernstein/blob/19672b2645438131a2a23b8992803ad5ef94b71e/src/bernstein/core/workflows/workflow_spec.py
[workflow-note]: https://github.com/sipyourdrink-ltd/bernstein/blob/19672b2645438131a2a23b8992803ad5ef94b71e/docs/release-notes/fragments/workflow-run-abort-skips-later-layers.md
