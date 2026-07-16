"""Cost-aware scheduling: USD budgets, pools, batch and cache policies (#2354).

A deterministic cost policy layer. Every decision is a pure function of a
hash-pinned price table, the existing spend ledger, and the policy config, so
two operators with the same ledger reproduce identical scheduling decisions and
can audit why any dispatch happened -- each budget decision is a journal-anchored
receipt.

Public surface:

* :mod:`.price_table` -- versioned, content-addressed USD price table (config
  overrides the shipped defaults; a staleness advisory feeds ``doctor``).
* :mod:`.pools` -- pool accounting projected from the ledger + pre-run
  exhaustion preflight (AC5).
* :mod:`.policy` -- the deterministic admit/halt dispatch decision (AC1, AC2).
* :mod:`.receipt` -- seals a decision into the lineage spine + audit chain and
  verifies it offline (the halt receipt is the proof, AC1).
* :mod:`.batch` -- capability-gated batch routing (AC3).
* :mod:`.cache_window` -- capability-gated, default-off cache-window fan-out
  (AC4).
* :mod:`.knob_matrix` -- versioned, content-addressed dispatch knob matrix
  (effort, processing lane, cache strategy) plus a pure resolver whose sealed
  selection folds into the decision fingerprint (#2519).
"""

from bernstein.core.cost.scheduling.batch import (
    BatchRouteDecision as BatchRouteDecision,
)
from bernstein.core.cost.scheduling.batch import (
    route_batch as route_batch,
)
from bernstein.core.cost.scheduling.cache_window import (
    CacheFanoutPlan as CacheFanoutPlan,
)
from bernstein.core.cost.scheduling.cache_window import (
    CacheFanoutResult as CacheFanoutResult,
)
from bernstein.core.cost.scheduling.cache_window import (
    execute_cache_fanout as execute_cache_fanout,
)
from bernstein.core.cost.scheduling.cache_window import (
    plan_cache_fanout as plan_cache_fanout,
)
from bernstein.core.cost.scheduling.dispatch_gate import (
    RunDispatchOutcome as RunDispatchOutcome,
)
from bernstein.core.cost.scheduling.dispatch_gate import (
    build_dispatch_candidates as build_dispatch_candidates,
)
from bernstein.core.cost.scheduling.dispatch_gate import (
    evaluate_run_dispatch as evaluate_run_dispatch,
)
from bernstein.core.cost.scheduling.dispatch_gate import (
    resolve_cost_caps as resolve_cost_caps,
)
from bernstein.core.cost.scheduling.dispatch_gate import (
    resolve_knob_matrix as resolve_knob_matrix,
)
from bernstein.core.cost.scheduling.dispatch_gate import (
    resolve_price_table as resolve_price_table,
)
from bernstein.core.cost.scheduling.knob_matrix import (
    DEFAULT_KNOB_MATRIX as DEFAULT_KNOB_MATRIX,
)
from bernstein.core.cost.scheduling.knob_matrix import (
    KnobMatrix as KnobMatrix,
)
from bernstein.core.cost.scheduling.knob_matrix import (
    ModelKnobs as ModelKnobs,
)
from bernstein.core.cost.scheduling.knob_matrix import (
    knob_matrix_staleness as knob_matrix_staleness,
)
from bernstein.core.cost.scheduling.knob_matrix import (
    load_knob_matrix as load_knob_matrix,
)
from bernstein.core.cost.scheduling.knob_matrix import (
    resolve_knob_selection as resolve_knob_selection,
)
from bernstein.core.cost.scheduling.policy import (
    CostCaps as CostCaps,
)
from bernstein.core.cost.scheduling.policy import (
    DispatchCandidate as DispatchCandidate,
)
from bernstein.core.cost.scheduling.policy import (
    DispatchDecision as DispatchDecision,
)
from bernstein.core.cost.scheduling.policy import (
    KnobSelection as KnobSelection,
)
from bernstein.core.cost.scheduling.policy import (
    LedgerSpend as LedgerSpend,
)
from bernstein.core.cost.scheduling.policy import (
    decide_dispatch as decide_dispatch,
)
from bernstein.core.cost.scheduling.policy import (
    project_spend as project_spend,
)
from bernstein.core.cost.scheduling.pools import (
    PoolExhaustion as PoolExhaustion,
)
from bernstein.core.cost.scheduling.pools import (
    PoolPreflightReport as PoolPreflightReport,
)
from bernstein.core.cost.scheduling.pools import (
    preflight_pools as preflight_pools,
)
from bernstein.core.cost.scheduling.pools import (
    project_pools as project_pools,
)
from bernstein.core.cost.scheduling.price_table import (
    DEFAULT_PRICE_TABLE as DEFAULT_PRICE_TABLE,
)
from bernstein.core.cost.scheduling.price_table import (
    ModelPrice as ModelPrice,
)
from bernstein.core.cost.scheduling.price_table import (
    PriceTable as PriceTable,
)
from bernstein.core.cost.scheduling.price_table import (
    load_price_table as load_price_table,
)
from bernstein.core.cost.scheduling.price_table import (
    price_table_staleness as price_table_staleness,
)
from bernstein.core.cost.scheduling.receipt import (
    DispatchReceipt as DispatchReceipt,
)
from bernstein.core.cost.scheduling.receipt import (
    build_dispatch_receipt as build_dispatch_receipt,
)
from bernstein.core.cost.scheduling.receipt import (
    verify_dispatch_receipt as verify_dispatch_receipt,
)
