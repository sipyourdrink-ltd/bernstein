Closes #5337

## Summary
`price_model_usage` logged the "no pricing-table entry" warning on every
call for an unpriced model/route name, flooding logs in non-interactive
runs. Cost-estimate lines also showed `$0.00` for these, implying free
rather than unpriced.

## Changes
- `model_prices.py` — module-level `_WARNED_UNPRICED_MODELS` set gates the
  warning to once per distinct model name per process. No change to
  wording, log level, or metering; `priced` stays `False`, tokens still
  count in totals.
- `run_preflight.py`, `bootstrap.py`, `run_bootstrap.py` — cost-estimate
  lines print `unpriced` instead of `$0.00` when `priced=False`. Free
  routes (`:free` suffix / free adapters) unaffected.
- `test_cost_price_model_usage.py` — new tests: dedup same name, dedup
  per distinct name, unpriced display (bootstrap + preflight), priced
  display unaffected.
- `test_openai_agents.py` — clears warned-set before its test to avoid
  cross-test leakage.
- `docs/release-notes/fragments/5337-cost-warning-dedup.md` — release note.

## Acceptance criteria
- [x] Same unpriced name warns once; repeat calls silent.
- [x] Different unpriced names each warn once.
- [x] Unpriced cost-estimate lines read `unpriced`, not `$0.00`; priced
      models unaffected.
- [x] `priced` stays `False`; totals still include unpriced tokens.

## Out of scope
Token metering, `MODEL_COSTS_PER_1M_TOKENS`, no config flags, no
persistence for the warned-names set.

## Testing
```
uv run pytest tests/unit/test_cost_price_model_usage.py \
  tests/unit/test_issue_3013_free_route_banner_pricing.py \
  tests/unit/test_cost_engine.py \
  tests/unit/cost/test_model_call_ledger.py -q
# 90 passed

uv run ruff check src tests
# clean
```
Pre-existing Windows failure (`test_append_tokens_sidecar_survives_unwritable_path`)
also fails on `main`, unrelated to this change.