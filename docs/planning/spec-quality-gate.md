# Spec-quality checklist gate

The spec-quality gate evaluates a feature spec against a small set of
deterministic content rules before the orchestrator dispatches an
implementer agent. Specs that fail any required rule are routed through
an auto-fix loop with a bounded number of attempts; when the loop
exhausts its budget the gate refuses to advance and surfaces the
checklist report to the operator.

## Why the gate exists

Tasks generated from a thin spec produce thin implementations and
re-work loops. Catching the gap at spec time avoids spending agent
context on under-specified work. Because the gate runs locally and is
purely text-driven it has no LLM cost and adds milliseconds to the
planning path.

## Default rules

| Rule id | Required | What it checks |
|---|---|---|
| `acceptance_criteria_present` | yes | The spec has an `## Acceptance criteria` heading. |
| `out_of_scope_present` | yes | The spec has an `## Out of scope` heading. |
| `tested_via_present` | yes | The spec mentions how the change is tested. |
| `no_todo_markers` | yes | No `TODO` markers remain in the spec body. |
| `no_placeholder_tokens` | yes | No `<PLACEHOLDER>`, `TBD`, or `XXX` tokens remain. |
| `ref_paths_exist` | yes | Every backtick-quoted path with a `/` separator exists in the workspace. |

All rules are pluggable; project plugins register additional rules via
the `bernstein.spec_quality_rules` entry-point group. Each entry must
resolve to a zero-arg callable that returns a
`bernstein.core.planning.spec_quality.Rule`. Optional rules
(`required=False`) are reported but never block the gate.

## CLI surface

```
bernstein spec check <path>            # evaluate, exit 2 on failure
bernstein spec check <path> --no-strict
bernstein spec auto-fix <path>         # heuristic fix loop, dry-run by default
bernstein spec auto-fix <path> --write
```

`--max-iter` (default `3`) controls how many auto-fix iterations are
attempted before the gate refuses to advance. The local heuristic
patcher adds missing section headings and rewrites stray `TODO`
markers; semantic gaps (e.g. a missing referenced file) require
operator intervention.

## Pipeline integration

Orchestrator code that dispatches an implementer agent should wrap the
call site with `refuse_to_advance`:

```python
from bernstein.core.planning.spec_quality import (
    SpecQualityUnresolvedError,
    refuse_to_advance,
)

try:
    report = refuse_to_advance(
        spec_path,
        workspace_root=repo_root,
        autofix=heuristic_or_llm_fixer,
        max_iterations=3,
    )
except SpecQualityUnresolvedError as exc:
    # Surface ``exc.report`` to the operator; do not dispatch the
    # implementer.
    return
```

`refuse_to_advance` returns the final report when the gate passes and
raises `SpecQualityUnresolvedError` when it does not. The exception
carries the last `ChecklistReport` so callers can render the failed
items without re-evaluating the spec.

## Authoring new rules

A rule is a small dataclass containing an id, description, and a
callable:

```python
from bernstein.core.planning.spec_quality import Rule, RuleResult


def _check(spec_text, workspace_root):
    if "stakeholder" not in spec_text.lower():
        return RuleResult(
            rule_id="stakeholder_named",
            passed=False,
            message="Spec does not name a stakeholder.",
            hint="Add a 'Stakeholder' heading or sentence.",
        )
    return RuleResult(rule_id="stakeholder_named", passed=True)


def factory() -> Rule:
    return Rule(
        rule_id="stakeholder_named",
        description="Spec names a stakeholder.",
        check=_check,
    )
```

Register the factory in your plugin's `pyproject.toml`:

```toml
[project.entry-points."bernstein.spec_quality_rules"]
stakeholder_named = "my_pkg.spec_rules:factory"
```

Plugins discovered at startup are merged after the default rule set;
rule ids must be unique across the registry. Rules that raise during
evaluation are converted into a failing `RuleResult` so a broken plugin
cannot crash the gate.
