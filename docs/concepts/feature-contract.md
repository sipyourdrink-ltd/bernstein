# Feature contract

A feature contract is an immutable list of features stored at
`.sdd/contract/features.json`. Each feature has an id, a description, an
acceptance check, and a `passes` flag. Agents may flip `passes: true`
only when the declared acceptance check actually exits zero. The list's
canonical sha256 is kept in the file's `anchor` field so
`FeatureContract.load` detects in-place edits. `features_from_plan_step()`
can build the list from a plan step's `features:` key and
`record_anchor()` can write the anchor into the audit log, but neither is
called automatically by the plan loader or orchestrator.

## Why it exists

Two failure modes show up in long-running self-evolution runs:

1. **Premature victory** - agent completes 6 of 10 features and calls
   `POST /tasks/{id}/complete`, declaring the task done.
2. **Test deletion** - agent "passes" by weakening or removing the
   failing test rather than fixing the code.

The feature contract is the immutable spec the agent reads but cannot
meaningfully game, and the per-feature pass/fail board that survives
across sessions.

## How to use it

The contract is a JSON document at `.sdd/contract/features.json`. Each
entry is a `Feature` with `id`, `category`, `description`,
`acceptance_steps`, `acceptance_check`, and a `passes` flag:

```json
{
  "schema_version": 1,
  "anchor": "<sha256 of the canonical features list>",
  "features": [
    {
      "id": "jwt-issue",
      "category": "auth",
      "description": "POST /auth/login returns a JWT",
      "acceptance_steps": ["Login with valid creds"],
      "acceptance_check": "pytest tests/auth/test_login.py::test_jwt_issued"
    },
    {
      "id": "revocation",
      "category": "auth",
      "description": "Revoked refresh tokens cannot be reused",
      "acceptance_check": "pytest tests/auth/test_revocation.py"
    }
  ]
}
```

Load and verify the contract programmatically:

```python
from bernstein.core.planning.feature_contract import FeatureContract
from bernstein.core.planning.spec_assertions import verify_contract

contract = FeatureContract.load()  # raises on tampering
# allow_subprocess=True lets `acceptance_check` commands actually run;
# by default they are reported as failed. Returns None if no contract file exists.
extraction, results = verify_contract(allow_subprocess=True, apply=True)  # run checks, flip passes
```

The contract feeds the [spec-as-test loop](spec-as-test.md): each
feature's `acceptance_steps` and `acceptance_check` are compiled into
executable assertions by `spec_assertions`.

## How tamper-detection works

The contract is persisted at `.sdd/contract/features.json`. Its
canonical sha256 is stored as the `anchor` field (`compute_anchor`);
loading via `FeatureContract.load` raises `TamperingDetectedError` when
the stored anchor no longer matches the features list, so an in-place
edit by an agent is detected on the next load.

`verify_contract` re-runs each `acceptance_check` and writes the
`passes` flag per feature, so a check that has been weakened or deleted
flips back to failing.

## Configuration

There are no user-facing configuration knobs. The contract path defaults
to `.sdd/contract/features.json` and `verify_contract` takes
`contract_path`, `repo_root`, `allow_subprocess` and `apply` arguments.

## Limitations

- The operator authors `acceptance_steps` and `acceptance_check`.
- Contracts live with the plan; there is no cross-project feature
  library.
- No CLI or visual board UI; read `features.json` directly.
- Acceptance checks run as an argv (`shlex.split`, no shell) via
  `subprocess.run` in the repo root, only when `allow_subprocess=True`,
  with a 30 s timeout; no command allowlist is applied, so supply them
  with care.

## Related

- Source: `src/bernstein/core/planning/feature_contract.py`
- Assertions: `src/bernstein/core/planning/spec_assertions.py`
- Audit anchor: `FeatureContract.record_anchor` (writes a `feature_contract.anchor` audit-log event)
- PR #997
