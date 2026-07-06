# Verifiable governance: RBAC, budgets, and seats

For teams, Bernstein expresses **role-based access control**, **budget
enforcement**, and **per-seat attribution** as deterministic projections over
the signed lineage spine rather than mutable, separately-logged database state.
Each governance decision is a signed, anchored record: strip the spine and it is
just a file; anchored, every decision recomputes offline and is independently
verifiable rather than merely recorded.

Module: `src/bernstein/core/security/governance.py`. CLI group:
`bernstein governance`.

## The decision record

Every access and budget decision is one record binding:

| Field | Meaning |
|---|---|
| `subject` | The seat / actor / user id the decision is about. |
| `action` | The requested permission string, or `budget` for a budget check. |
| `verdict` | `allow` / `deny` (access) or `allow` / `refuse` (budget). |
| `inputs_hash` | Content hash of the projection inputs the verdict was derived from. |
| `journal_entry_hash` | The lineage-spine entry hash over the record bytes: its chain-verifiable identity. |

Records land under `.sdd/lineage/<run>/governance_decisions/`, colocated with
the run's spine so the record and its anchor share one root.

## Access control (RBAC)

`decide_access` resolves a subject's IDP groups to a role via a **signed
`RoleBindings`** (IDP-group -> role, role -> permissions), then projects the
role's permissions onto the requested action. When the role grants the action
the verdict is `allow`; otherwise a signed `deny` record is written. A denied
action is still a signed, anchored record - not merely a log line. When a
subject's groups map to more than one role, the highest-privilege role wins
(`admin` > `operator` > `viewer`).

## Budgets

`check_budget_decision` recomputes the subject's cumulative spend from the cost
ledger (`.sdd/cost/ledger.jsonl`), never a stored counter. When prior spend plus
the next call would breach the per-subject cap, a signed `refuse` record is
written and the action is blocked (`BudgetRefused`). The operator policy inputs
(`cap_usd`, `next_cost_usd`) are carried in the record so a verifier can
recompute the verdict; the ledger-derived part - prior spend - is re-projected
at verify time.

## Seat / cost attribution

`seat_spend(ledger_path, subject)` is a pure projection over the ledger rows on
disk: every row is re-read and the matching subject's `cost_usd` summed. Two
operators holding the same ledger compute the byte-identical total. The
attribution dimension (`agent` / `task` / `role` / `feature_label`) is
selectable; `agent` is the per-seat default.

## Guarantees

- **Verifiability** - `bernstein governance verify <run>` re-resolves and
  re-projects every recorded access and budget decision from the presented
  bindings and ledger and confirms the recorded verdicts. Any single-byte tamper
  to a record, the bindings, or the spine fails the check.
- **Correctness** - a budget breach writes a signed refusal and blocks the
  action; per-seat spend is recomputable from the ledger rather than a mutable
  counter.
- **Determinism** - every decision row is canonical JSON and every field is a
  pure function of caller input, so two replays of a governance-gated run
  produce byte-identical decision records. No LLM in the decision loop.

## CLI

```
bernstein governance verify <run> --bindings bindings.json [--ledger ledger.jsonl]
```

Exit codes: `0` verified, `1` no records / bad input, `2` mismatch.

The `--bindings` file is a signed `RoleBindings` JSON (`RoleBindings.to_dict()`).
`--ledger` is required when the run carries budget decisions. Each recorded
decision is also mirrored into the HMAC audit chain as a `governance.decision`
event, so an operator can confirm from the chain alone that a decision bound the
claimed inputs to a named spine entry.
