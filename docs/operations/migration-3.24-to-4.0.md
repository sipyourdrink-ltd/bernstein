# Migrating from 3.24 to 4.0

This guide compares Bernstein 3.24.1 with the merged behavior to check when
upgrading to 4.0. It covers seed configuration, gate plugins, MCP clients,
workflows, skill sources, and consumers of render deltas.

## Quality-gate seed parsing

In 3.24.1, the seed parser accepted some supported `quality_gates` keys but
left the corresponding gates at their defaults ([release note][gate-note]).
[`_parse_quality_gates`][gate-parser] now forwards `mutation_testing`,
`dep_audit`, `intent_verification`, `large_file_check`,
`migration_reversibility_check`, `test_expansion`, `agent_test_mutation`,
`integration_test_gen`, `review_rubric`, and their supported options to
`QualityGatesConfig`.

Upgrade checklist:

- Review gate values already present in your seed: they can now affect the
  configuration. In particular, the repository's [`bernstein.yaml`][seed] sets
  `large_file_check: true`, and that value now takes effect
  ([release note][gate-note], [parser][gate-parser]).
- Supply `quality_gates.intent_verification` as a mapping. A non-mapping value
  such as `true` raises `SeedError`. An omitted or null value uses
  `IntentVerificationConfig()`
  ([`_parse_quality_gate_intent`][intent-parser]).
- Use integers for integer fields. Boolean values such as `timeout_s: true` or
  `intent_verification.max_tokens: true` raise `SeedError`, even though Python
  treats `bool` as a subclass of `int`
  ([gate parser][gate-parser], [intent parser][intent-parser]).

**Compatibility limit:** omitted values retain the defaults in
[`QualityGatesConfig` and `IntentVerificationConfig`][gate-config].
`large_file_check` still defaults to `false`. Explicit seed values
override those defaults through the [parser][gate-parser].

## Generated quality gates

The [generated Python class][generated-gate] implements `GatePlugin`.
Its `run(changed_files, run_dir, task_title, task_description)` method returns
a `GateResult`, and `scan(changed_files, run_dir)` returns the error and
warning counts. The template returns a failing result with `blocked=False`.
[`GateRunner`][generated-runner] applies the pipeline step's `required` policy
and stamps the result with that value. For a failing generated gate,
`required=True` blocks promotion and `required=False` records an advisory
failure. A scan exception produces an `inconclusive` result with the same
blocking policy.

The [gate registry][generated-discovery] discovers local Python files in the
project's `.bernstein/gates/` directory and loads registered classes from the
`bernstein.gates` entry-point group.

Upgrade check

1. Generate the plugin and run `python -m pytest -q` from the generated
   directory with Bernstein installed, as described in the
   [generated README][generated-readme].
2. Copy the generated module to `.bernstein/gates/` or register its class under
   `bernstein.gates`. Use its name in a Python pipeline, for example:

   ```python
   from bernstein.core.quality.gate_pipeline import GatePipelineStep
   from bernstein.core.quality.quality_gates import QualityGatesConfig

   config = QualityGatesConfig(pipeline=[GatePipelineStep(name="my_gate", required=True, condition="any_changed")])
   ```

   Replace `my_gate` with the generated name and pass `config` to `GateRunner`.
   With a changed file and a scan that reports one error at the template's
   default threshold, check that the step reports `fail` and blocks. Set
   `required=False` and verify that the same failure becomes advisory.

**Compatibility limit:** the generated dataclass uses deferred annotations.
As the [generated README][generated-readme] notes, the local-file loader in
3.24.1 and earlier cannot load that dataclass reliably. Use a build containing
the [module-registration fix][generated-discovery]. The current
[`bernstein.yaml` pipeline parser][generated-seed-limit] only accepts names
in its built-in allowlist, so use a Python pipeline for a custom gate name.

## MCP SDK and task wire compatibility

Bernstein 3.24.1 declared [`mcp>=1.30.0,<2`][mcp-baseline]. The SDK upgrade
declares [`mcp>=2.3.0,<2.4`][mcp-dependency] and keeps the existing
`2025-11-25` task creation, polling, result, list, and cancellation wire
contract backed by Bernstein's persistent TaskStore
([release note][mcp-note]).

The [SDK compatibility middleware][mcp-bridge] runs the normal dispatcher
and rewrites a successful tool result into a legacy `CreateTaskResult` only
when a `tools/call` at `2025-11-25` includes a `task` object, names an optional
task tool, and returns structured content containing only `task`.
[`bernstein_run`][mcp-tools] is the optional task tool. Successful
task-augmented orchestration calls return the legacy result. Successful calls
with `template="administrative"` retain the ordinary result even when
task-augmented ([run routing][mcp-run-routing]). Plain calls also retain
ordinary tool results. The middleware adds the legacy
`tasks` capability block only when the `initialize` response negotiates
`2025-11-25`.

The custom streamable HTTP transport serves its four
[handshake-era revisions][mcp-http-versions] (`2024-11-05`, `2025-03-26`,
`2025-06-18`, and `2025-11-25`). With header checks enabled by default,
an explicitly unsupported `MCP-Protocol-Version` header gets
[HTTP 400 with the supported revisions][mcp-http-header]. The
[initialization handshake][mcp-http-init] can negotiate an implemented
revision. SDK2 clients in automatic mode can negotiate `2025-11-25`
([release note][mcp-note]).

Client upgrade check

| Request or result | Expected after the SDK upgrade |
| --- | --- |
| Plain `tools/call` without a `task` object | Ordinary tool result |
| Successful orchestration `bernstein_run` with a `task` object at `2025-11-25` | Legacy `CreateTaskResult` with a `task` |
| Successful `bernstein_run` with `template="administrative"` and a `task` object at `2025-11-25` | Ordinary tool result with the administrative run ID |
| `initialize` negotiating `2025-11-25` on the SDK server | Legacy `tasks` capability advertised |
| `initialize` negotiating another revision on the SDK server | Legacy `tasks` capability absent |
| Unsupported `MCP-Protocol-Version` header on custom HTTP with default checks | HTTP 400 |

**Compatibility limit:** use the declared SDK 2.3.x line. The adapter uses
the provisional middleware API, so the upper bound stays below 2.4 pending
another compatibility review. The retained wire is the `2025-11-25` contract.
Clients requiring the redesigned Tasks extension or a newer custom HTTP
revision need server support beyond this update ([release note][mcp-note]).

## Workflow failure boundaries

When a node fails, the [`WorkflowRunner.run` execution loop][workflow-run] sets
the run's abort state. Every later layer is skipped, including nodes whose own
dependencies succeeded. Those layers do not dispatch commands or agents, and
each skipped node is appended to the execution result once. This aligns `run`
with the existing `resume` failure boundary
([release note][workflow-note], [`resume` loop][workflow-resume]).

Before/after check: suppose `a` and `b` share a layer and `c` has
`depends_on: [b]`. If `a` fails and `b` succeeds, 3.24.1 could
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

## Skill source initialization

Bernstein 3.24.1 already
[isolated skill initialization failures in memory][skills-baseline]. The
current [`_reload` loop][skills-reload] still quarantines a source when its
`iter_skills()` raises, and quarantines individual artifacts when
registration fails. The other sources and artifacts continue loading.
[`_register`][skills-register] raises `DuplicateSkillError` for an existing
name, so the first successfully indexed definition remains selected and the
later artifact is quarantined.

The in-memory [`quarantined` property][skills-notify] exposes the
`QuarantinedSkill` records. If provided, `on_quarantine` receives each newly
recorded failure. The loader logs callback errors and continues. It restores
persisted markers without calling the hook ([`_reload`][skills-reload]).
A custom source's [`iter_skills()`][skills-source] returns a
`list[SkillArtifact]`. Return an empty list when it provides no skills.

The persistence behavior changes for configured workspaces. Bare
[`SkillLoader(sources)`][skills-init] still has no store and keeps its
records in memory. [`default_loader_from_templates(...)`][skills-factory]
now supplies a workspace `DeclarationQuarantineStore`, with `workdir`
defaulting to the current directory. The
[workspace marker file][skills-marker] survives restarts. On the next load,
a source marker prevents enumeration of that source. An artifact marker
skips that artifact after enumeration, leaving its siblings eligible.

Upgrade check: construct the loader in a test workspace with a source whose
`iter_skills()` raises and a healthy source. Check that the healthy skills
are available and that `loader.quarantined` includes a record for the
throwing source with `skill_name=None`. With the configured default loader,
recreate it for the same workspace and verify that it restores the
quarantine record without enumerating the marked source again.

**Compatibility limit:** direct library callers need to pass
`quarantine_store` to persist records. To detect a duplicate, inspect
`loader.quarantined` or use `on_quarantine`. The loader catches the
registration exception and keeps the first definition.

## Render delta serialization

The 3.24.1 serialized delta had no fields for the receipts' full snapshots.
[`RenderDelta`][render-delta] now carries `base_unstable_properties` and
`head_unstable_properties` as sorted tuples of property/value pairs.
[`render_delta`][render-compare] retains each receipt's full unstable-property
snapshot, including unchanged values, even when the environments are
incomparable and no property deltas are produced.

Consumer upgrade checklist:

- Accept the optional `base_unstable_properties` and
  `head_unstable_properties` objects in serialized deltas. `to_dict()` writes
  each field only when its snapshot is nonempty; it omits empty metadata
  fields ([`RenderDelta.to_dict`][render-delta]).
- Continue reading older deltas with those fields absent. `from_dict()` loads
  missing fields as empty tuples; present objects are converted to sorted
  string property/value pairs ([`RenderDelta.from_dict`][render-delta]).
- Keep the two snapshots available when displaying an incomparable result.
  Their presence does not mean that comparison succeeded
  ([`render_delta`][render-compare], [`RenderDelta.is_incomparable`][render-delta]).

[`first_property_difference(base, head)`][render-first] operates on flattened
observation maps. It returns the lexicographically first changed or missing
`(element_path, property)` tuple, ordered by element path and then property
name, or `None` for equal maps. It does not mutate either map or validate
environments; use [`render_delta`][render-compare] for receipt comparison with
environment validation.

**Compatibility limit:** this adds metadata to the serialized result; existing
environment checks and comparison verdicts are unchanged
([release note][render-note], [`render_delta`][render-compare]). Consumers that
require an exact set of JSON keys must allow the new optional fields. Missing
metadata can be read as empty by `from_dict()`, but metadata on an incomparable
result does not make the receipts comparable
([`RenderDelta` serialization][render-delta], [comparison][render-compare]).

[gate-parser]: https://github.com/sipyourdrink-ltd/bernstein/blob/6b02d787bf24f648e2829beecec87e913d37ff17/src/bernstein/core/config/seed_parser.py#L1856-L2003
[intent-parser]: https://github.com/sipyourdrink-ltd/bernstein/blob/6b02d787bf24f648e2829beecec87e913d37ff17/src/bernstein/core/config/seed_parser.py#L2062-L2109
[gate-config]: https://github.com/sipyourdrink-ltd/bernstein/blob/19672b2645438131a2a23b8992803ad5ef94b71e/src/bernstein/core/quality/quality_gates.py#L86-L294
[gate-note]: https://github.com/sipyourdrink-ltd/bernstein/blob/19672b2645438131a2a23b8992803ad5ef94b71e/docs/release-notes/fragments/6615-quality-gate-keys-from-seed.md
[seed]: https://github.com/sipyourdrink-ltd/bernstein/blob/19672b2645438131a2a23b8992803ad5ef94b71e/bernstein.yaml#L33-L42
[workflow-run]: https://github.com/sipyourdrink-ltd/bernstein/blob/19672b2645438131a2a23b8992803ad5ef94b71e/src/bernstein/core/workflows/workflow_runner.py#L623-L710
[workflow-resume]: https://github.com/sipyourdrink-ltd/bernstein/blob/19672b2645438131a2a23b8992803ad5ef94b71e/src/bernstein/core/workflows/workflow_runner.py#L461-L535
[workflow-result]: https://github.com/sipyourdrink-ltd/bernstein/blob/19672b2645438131a2a23b8992803ad5ef94b71e/src/bernstein/core/workflows/workflow_runner.py#L97-L154
[workflow-when]: https://github.com/sipyourdrink-ltd/bernstein/blob/19672b2645438131a2a23b8992803ad5ef94b71e/src/bernstein/core/workflows/workflow_runner.py#L853-L871
[workflow-dependency]: https://github.com/sipyourdrink-ltd/bernstein/blob/19672b2645438131a2a23b8992803ad5ef94b71e/src/bernstein/core/workflows/workflow_runner.py#L714-L726
[workflow-layer]: https://github.com/sipyourdrink-ltd/bernstein/blob/19672b2645438131a2a23b8992803ad5ef94b71e/src/bernstein/core/workflows/workflow_runner.py#L728-L761
[workflow-spec]: https://github.com/sipyourdrink-ltd/bernstein/blob/19672b2645438131a2a23b8992803ad5ef94b71e/src/bernstein/core/workflows/workflow_spec.py#L303-L329
[workflow-note]: https://github.com/sipyourdrink-ltd/bernstein/blob/19672b2645438131a2a23b8992803ad5ef94b71e/docs/release-notes/fragments/workflow-run-abort-skips-later-layers.md
[render-delta]: https://github.com/sipyourdrink-ltd/bernstein/blob/19672b2645438131a2a23b8992803ad5ef94b71e/src/bernstein/core/evidence/render_receipt.py#L643-L713
[render-compare]: https://github.com/sipyourdrink-ltd/bernstein/blob/19672b2645438131a2a23b8992803ad5ef94b71e/src/bernstein/core/evidence/render_receipt.py#L743-L805
[render-first]: https://github.com/sipyourdrink-ltd/bernstein/blob/19672b2645438131a2a23b8992803ad5ef94b71e/src/bernstein/core/evidence/render_receipt.py#L571-L588
[render-note]: https://github.com/sipyourdrink-ltd/bernstein/blob/19672b2645438131a2a23b8992803ad5ef94b71e/docs/release-notes/fragments/7088-render-pair-unstable-properties.md
[generated-gate]: https://github.com/sipyourdrink-ltd/bernstein/blob/6b02d787bf24f648e2829beecec87e913d37ff17/templates/quality_gate_plugin/%7B%7Bcookiecutter.gate_name%7D%7D/%7B%7Bcookiecutter.gate_name%7D%7D.py#L36-L118
[generated-runner]: https://github.com/sipyourdrink-ltd/bernstein/blob/6b02d787bf24f648e2829beecec87e913d37ff17/src/bernstein/core/quality/gate_runner.py#L452-L496
[generated-discovery]: https://github.com/sipyourdrink-ltd/bernstein/blob/6b02d787bf24f648e2829beecec87e913d37ff17/src/bernstein/core/quality/gate_plugins.py#L62-L132
[generated-readme]: https://github.com/sipyourdrink-ltd/bernstein/blob/6b02d787bf24f648e2829beecec87e913d37ff17/templates/quality_gate_plugin/%7B%7Bcookiecutter.gate_name%7D%7D/README.md#L3-L28
[generated-seed-limit]: https://github.com/sipyourdrink-ltd/bernstein/blob/6b02d787bf24f648e2829beecec87e913d37ff17/src/bernstein/core/config/seed_parser.py#L2006-L2017
[mcp-baseline]: https://github.com/sipyourdrink-ltd/bernstein/blob/9c27d9977227594dd40dd2820153256c9500421e/pyproject.toml#L72-L77
[mcp-dependency]: https://github.com/sipyourdrink-ltd/bernstein/blob/6b02d787bf24f648e2829beecec87e913d37ff17/pyproject.toml#L73-L80
[mcp-bridge]: https://github.com/sipyourdrink-ltd/bernstein/blob/6b02d787bf24f648e2829beecec87e913d37ff17/src/bernstein/mcp/sdk_compat.py#L20-L62
[mcp-tools]: https://github.com/sipyourdrink-ltd/bernstein/blob/6b02d787bf24f648e2829beecec87e913d37ff17/src/bernstein/mcp/server.py#L2393-L2403
[mcp-run-routing]: https://github.com/sipyourdrink-ltd/bernstein/blob/6b02d787bf24f648e2829beecec87e913d37ff17/src/bernstein/mcp/server.py#L713-L743
[mcp-http-versions]: https://github.com/sipyourdrink-ltd/bernstein/blob/6b02d787bf24f648e2829beecec87e913d37ff17/src/bernstein/mcp/remote_transport.py#L93-L102
[mcp-http-header]: https://github.com/sipyourdrink-ltd/bernstein/blob/6b02d787bf24f648e2829beecec87e913d37ff17/src/bernstein/mcp/remote_transport.py#L650-L672
[mcp-http-init]: https://github.com/sipyourdrink-ltd/bernstein/blob/6b02d787bf24f648e2829beecec87e913d37ff17/src/bernstein/mcp/remote_transport.py#L883-L914
[mcp-note]: https://github.com/sipyourdrink-ltd/bernstein/blob/19672b2645438131a2a23b8992803ad5ef94b71e/docs/release-notes/fragments/6619-mcp-23-compatibility.md
[skills-baseline]: https://github.com/sipyourdrink-ltd/bernstein/blob/9c27d9977227594dd40dd2820153256c9500421e/src/bernstein/core/skills/loader.py#L227-L278
[skills-reload]: https://github.com/sipyourdrink-ltd/bernstein/blob/6b02d787bf24f648e2829beecec87e913d37ff17/src/bernstein/core/skills/loader.py#L255-L308
[skills-register]: https://github.com/sipyourdrink-ltd/bernstein/blob/6b02d787bf24f648e2829beecec87e913d37ff17/src/bernstein/core/skills/loader.py#L320-L348
[skills-notify]: https://github.com/sipyourdrink-ltd/bernstein/blob/6b02d787bf24f648e2829beecec87e913d37ff17/src/bernstein/core/skills/loader.py#L195-L253
[skills-source]: https://github.com/sipyourdrink-ltd/bernstein/blob/6b02d787bf24f648e2829beecec87e913d37ff17/src/bernstein/core/skills/source.py#L44-L69
[skills-init]: https://github.com/sipyourdrink-ltd/bernstein/blob/6b02d787bf24f648e2829beecec87e913d37ff17/src/bernstein/core/skills/loader.py#L146-L178
[skills-factory]: https://github.com/sipyourdrink-ltd/bernstein/blob/6b02d787bf24f648e2829beecec87e913d37ff17/src/bernstein/core/skills/loader.py#L430-L474
[skills-marker]: https://github.com/sipyourdrink-ltd/bernstein/blob/6b02d787bf24f648e2829beecec87e913d37ff17/docs/operations/declaration-quarantine.md#L3-L15
