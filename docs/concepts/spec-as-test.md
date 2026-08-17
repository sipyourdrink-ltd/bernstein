# Spec-as-test loop

The [feature contract](feature-contract.md) describes acceptance
criteria as free-text steps. The `spec_assertions` module turns those
steps into **executable assertions** (file-exists / import-resolves /
regex-in-file / test-passes), runs them after each stage drain, and
routes failures back as auto-fix tasks or human-review bulletins.

## Why it exists

Before this loop, "spec said X, code does X" was a human re-read of
the plan. Silent drift between plan intent and merged code went
unnoticed until a human noticed. This pattern complements the
[feature contract](feature-contract.md) - that one freezes the WHAT,
spec-as-test verifies the IS.

## How to use it

Assertions are derived from the feature contract at
`.sdd/contract/features.json`. Each `Feature` carries free-text
`acceptance_steps` plus an `acceptance_check`:

```json
{
  "features": [
    {
      "id": "healthz",
      "description": "Add /healthz endpoint",
      "acceptance_steps": [
        "exists src/api/healthz.py",
        "import api.healthz",
        "contains src/api/__init__.py /from .healthz import/"
      ],
      "acceptance_check": "pytest tests/api/test_healthz.py"
    }
  ]
}
```

`extract_assertions(contract)` parses each feature into typed
`Assertion` records, returned inside an `AssertionExtractionReport`
(with `.assertions`, `.unparsed`, and `.skipped_features`). The
`acceptance_check` becomes a `test_passes` assertion; each parseable
step becomes a `file_exists` / `import_resolves` / `regex_in_file`
assertion. `run_assertions(assertions, repo_root)` executes them after
every stage drain; `verify_contract()` is the top-level entry point.
Failures post a bulletin and create an auto-fix task targeting the
offending feature.

You can emit the assertions as a real pytest file for CI:

```python
from pathlib import Path
from bernstein.core.planning.spec_assertions import (
    load_contract,
    extract_assertions,
    assertions_to_pytest,
)

contract = load_contract()
report = extract_assertions(contract)
assertions_to_pytest(report.assertions, out_path=Path("tests/spec/test_plan_jwt.py"))
```

## Supported assertion kinds

The step grammar parsed out of `acceptance_steps`:

| Step syntax | Kind | Predicate |
|---|---|---|
| `exists <path>` (or `file exists <path>`) | `file_exists` | path resolves to a regular file |
| `import <module>` | `import_resolves` | dotted import succeeds in the project venv |
| `contains <path> /<regex>/` | `regex_in_file` | regex matches at least once in the file's bytes |
| (the feature's `acceptance_check`) | `test_passes` | the command / pytest selector exits 0 |

Steps that match no rule land in the report's `unparsed` list rather
than running.

## Configuration

| Knob | Default | Controls |
|---|--:|---|
| `spec_assertions.enabled` | `true` | Master switch (CLI: `--no-spec-test`). |
| `spec_assertions.run_on_drain` | `true` | Run after every stage drain. |
| `spec_assertions.emit_pytest` | `false` | Also write pytest files to `tests/spec/`. |

## Limitations

- Only the four kinds listed above. Property-based testing,
  Gherkin/BDD, and LLM-generated assertions are out of scope.
- Assertions are derived from the feature contract's
  `acceptance_steps` / `acceptance_check`; no separate spec format.
- The pytest emitter writes synchronous tests; async-only test suites
  need a custom runner wrap.
- Failures attach an auto-fix task but never block merge by themselves
  - the existing janitor + quality gates remain the merge gate.

## Related

- Source: `src/bernstein/core/planning/spec_assertions.py`
- Drain hook: `src/bernstein/core/orchestration/drain.py`
- Run flag: `src/bernstein/cli/run_cmd.py`
- PR #1003
