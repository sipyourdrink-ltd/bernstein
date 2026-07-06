# Feature contract

A plan step can carry an immutable list of features. Each feature has
an id, a description, an acceptance check, and a `passes` flag. Agents
may flip `passes: true` only when the declared acceptance check
actually exits zero. The list is hash-anchored in the audit chain so
agents cannot quietly add, remove, or weaken entries.

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

contract = FeatureContract.load()              # raises on tampering
extraction, results = verify_contract(apply=True)  # run checks, flip passes
```

The contract feeds the [spec-as-test loop](spec-as-test.md): each
feature's `acceptance_steps` and `acceptance_check` are compiled into
executable assertions and run after each stage drain.

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

| Knob | Default | Controls |
|---|--:|---|
| `features.completion_blocks_on_failure` | `true` | Reject `complete` if any feature is `passes: false`. |
| `features.allow_partial_flag_required` | `true` | Only the operator-level `--allow-partial` overrides. |
| `features.janitor_recheck_interval_s` | `0` (every drain) | How often the janitor re-runs acceptance checks. |

## Limitations

- The operator authors `acceptance_steps` and `acceptance_check`.
- Contracts live with the plan; there is no cross-project feature
  library.
- CLI table output only - no visual board UI.
- Acceptance checks run as shell commands; supply them with care
  (the existing command allowlist still applies).

## Related

- Source: `src/bernstein/core/planning/feature_contract.py`
- Audit hook: `src/bernstein/core/security/audit.py`
- Janitor integration: `src/bernstein/core/quality/janitor.py`
- PR #997
