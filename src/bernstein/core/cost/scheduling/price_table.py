"""Versioned, hash-pinned USD price table for cost-aware scheduling (#2354).

Token-denominated budgets break when a provider changes tokenizers or ships a
new model; the scheduling layer needs a *stable, versioned, content-addressed*
map from model to USD rate so that a dispatch decision is a pure function of a
pinned table (no network lookup inside the scheduling loop).

This module is the price half of that contract:

* :class:`PriceTable` is an immutable ``model -> ModelPrice`` map plus an
  ``as_of`` date and a monotonic ``revision``. Its :meth:`~PriceTable.content_hash`
  is a canonical-JSON SHA-256 that pins exactly which rates a decision used.
* :data:`DEFAULT_PRICE_TABLE` is shipped from the existing leaf pricing table
  (:mod:`bernstein.core.cost.model_prices`) so the default is the same list
  prices the ledger already meters against -- one source of truth.
* :func:`load_price_table` builds a table from validated config rates so an
  operator can override the shipped defaults without a code change.
* :func:`price_table_staleness` powers the ``doctor`` advisory: a table older
  than a window is flagged (prices drift between releases) without blocking.

Determinism: :meth:`~PriceTable.price_call` matches the longest model key
first (so ``claude-sonnet-5`` beats the generic ``sonnet`` stem) and returns an
explicit ``$0`` with ``priced=False`` for an unknown model rather than a silent
drop -- identical semantics to
:func:`bernstein.core.cost.model_prices.price_model_usage`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any

from bernstein.core.cost.model_prices import MODEL_COSTS_PER_1M_TOKENS

if TYPE_CHECKING:
    from collections.abc import Mapping

#: ``as_of`` date shipped with :data:`DEFAULT_PRICE_TABLE`. Mirrors the
#: "Updated" stamp in :mod:`bernstein.core.cost.model_prices`; bump both
#: together when the shipped rates change.
DEFAULT_PRICE_TABLE_AS_OF = "2026-05-05"

#: Default window after which a price table is advised stale (prices drift
#: between releases).
DEFAULT_STALENESS_WINDOW_DAYS = 90


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """USD rate for one model, per 1 million tokens."""

    input_usd_per_1m: float
    output_usd_per_1m: float
    cache_read_usd_per_1m: float = 0.0
    cache_write_usd_per_1m: float = 0.0

    def to_dict(self) -> dict[str, float]:
        return {
            "input": self.input_usd_per_1m,
            "output": self.output_usd_per_1m,
            "cache_read": self.cache_read_usd_per_1m,
            "cache_write": self.cache_write_usd_per_1m,
        }


@dataclass(frozen=True, slots=True)
class PricedCall:
    """Result of pricing one call against a :class:`PriceTable`."""

    model: str
    cost_usd: float
    priced: bool


@dataclass(frozen=True)
class PriceTable:
    """Immutable, content-addressed USD price table.

    Attributes:
        models: Map of model key (matched case-insensitively as a substring,
            longest key first) to its :class:`ModelPrice`.
        as_of: ISO date (``YYYY-MM-DD``) the rates were captured.
        revision: Monotonic revision counter, bumped on any rate change.
    """

    models: dict[str, ModelPrice]
    as_of: str = DEFAULT_PRICE_TABLE_AS_OF
    revision: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of,
            "revision": self.revision,
            "models": {name: price.to_dict() for name, price in self.models.items()},
        }

    def _canonical_bytes(self) -> bytes:
        # Sorted keys make the hash independent of insertion order.
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")

    def content_hash(self) -> str:
        """``sha256:`` digest pinning exactly these rates + version metadata."""
        return "sha256:" + hashlib.sha256(self._canonical_bytes()).hexdigest()

    def price_call(
        self,
        model: str,
        *,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> PricedCall:
        """Price one call's token usage; longest matching key wins.

        An unknown model returns ``cost_usd == 0.0`` with ``priced=False`` --
        an explicit, visible zero (tokens are still the caller's to count),
        never a silent drop or a heuristic guess.
        """
        model_lower = model.lower()
        for key in sorted(self.models, key=len, reverse=True):
            if key.lower() in model_lower:
                price = self.models[key]
                cost = (
                    (input_tokens / 1_000_000.0) * price.input_usd_per_1m
                    + (output_tokens / 1_000_000.0) * price.output_usd_per_1m
                    + (cache_read_tokens / 1_000_000.0) * price.cache_read_usd_per_1m
                    + (cache_write_tokens / 1_000_000.0) * price.cache_write_usd_per_1m
                )
                return PricedCall(model=model, cost_usd=cost, priced=True)
        return PricedCall(model=model, cost_usd=0.0, priced=False)


def _coerce_rates(raw: Mapping[str, Any], *, key: str) -> ModelPrice:
    """Validate one config rate row into a :class:`ModelPrice`.

    Raises:
        ValueError: A rate is missing, non-numeric, or negative. A negative
            USD rate is a misconfiguration that must fail loudly rather than
            corrupt every downstream budget decision.
    """

    def _rate(field: str, *, required: bool) -> float:
        if field not in raw:
            if required:
                raise ValueError(f"price for model {key!r} is missing required rate {field!r}")
            return 0.0
        try:
            value = float(raw[field])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"price for model {key!r} has non-numeric rate {field!r}: {raw[field]!r}") from exc
        if value < 0:
            raise ValueError(f"price for model {key!r} has negative rate {field!r}={value}")
        return value

    return ModelPrice(
        input_usd_per_1m=_rate("input", required=True),
        output_usd_per_1m=_rate("output", required=True),
        cache_read_usd_per_1m=_rate("cache_read", required=False),
        cache_write_usd_per_1m=_rate("cache_write", required=False),
    )


def _default_models() -> dict[str, ModelPrice]:
    """Build the shipped model map from the leaf pricing table."""
    models: dict[str, ModelPrice] = {}
    for key, pricing in MODEL_COSTS_PER_1M_TOKENS.items():
        models[key] = ModelPrice(
            input_usd_per_1m=float(pricing.get("input", 0.0) or 0.0),
            output_usd_per_1m=float(pricing.get("output", 0.0) or 0.0),
            cache_read_usd_per_1m=float(pricing.get("cache_read") or 0.0),
            cache_write_usd_per_1m=float(pricing.get("cache_write") or 0.0),
        )
    return models


#: Shipped default price table, sourced from the existing leaf pricing table so
#: the scheduler prices identically to the ledger. Hash-pinned via
#: :meth:`PriceTable.content_hash`.
DEFAULT_PRICE_TABLE = PriceTable(models=_default_models(), as_of=DEFAULT_PRICE_TABLE_AS_OF, revision=1)


def load_price_table(
    models: Mapping[str, Mapping[str, Any]] | None = None,
    *,
    as_of: str | None = None,
    revision: int = 0,
    base: PriceTable | None = None,
) -> PriceTable:
    """Build a :class:`PriceTable` from validated config rates.

    When *models* is ``None`` or empty the shipped :data:`DEFAULT_PRICE_TABLE`
    is returned unchanged. Otherwise the config rows override / extend *base*
    (the shipped defaults by default), so an operator names only the models
    they want to re-rate.

    Args:
        models: Config ``model -> {input, output, cache_read?, cache_write?}``
            rate rows.
        as_of: ISO date the operator captured the override rates.
        revision: Monotonic revision counter for the resulting table.
        base: Table the overrides extend; defaults to the shipped table.

    Raises:
        ValueError: A rate is missing, non-numeric, or negative.
    """
    base_table = base if base is not None else DEFAULT_PRICE_TABLE
    if not models:
        return base_table
    merged: dict[str, ModelPrice] = base_table.models.copy()
    for key, raw in models.items():
        merged[key] = _coerce_rates(raw, key=key)
    return PriceTable(
        models=merged,
        as_of=as_of or base_table.as_of,
        revision=revision,
    )


@dataclass(frozen=True, slots=True)
class StalenessAdvisory:
    """Result of a price-table staleness check (drives the doctor advisory)."""

    stale: bool
    age_days: int
    as_of: str
    message: str


def _parse_iso_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def price_table_staleness(
    table: PriceTable,
    *,
    now_iso: str,
    max_age_days: int = DEFAULT_STALENESS_WINDOW_DAYS,
) -> StalenessAdvisory:
    """Return whether *table* is older than ``max_age_days`` as of ``now_iso``.

    A malformed ``as_of`` is treated as stale (fail-visible): an unparseable
    date cannot be proven fresh. The advisory never raises and never blocks --
    it is a doctor hint, mitigating price drift between releases.
    """
    now = _parse_iso_date(now_iso)
    captured = _parse_iso_date(table.as_of)
    if now is None:
        return StalenessAdvisory(
            stale=False,
            age_days=0,
            as_of=table.as_of,
            message=f"price table as_of={table.as_of!r}: cannot evaluate staleness (bad now {now_iso!r})",
        )
    if captured is None:
        return StalenessAdvisory(
            stale=True,
            age_days=max_age_days + 1,
            as_of=table.as_of,
            message=f"price table as_of={table.as_of!r} is not a valid ISO date; treat rates as stale",
        )
    age_days = (now - captured).days
    stale = age_days > max_age_days
    if stale:
        message = (
            f"price table as_of={table.as_of} is {age_days}d old (> {max_age_days}d); "
            "provider rates may have drifted -- refresh cost_policy.pricing or the shipped table"
        )
    else:
        message = f"price table as_of={table.as_of} is {age_days}d old (<= {max_age_days}d)"
    return StalenessAdvisory(stale=stale, age_days=age_days, as_of=table.as_of, message=message)


__all__ = [
    "DEFAULT_PRICE_TABLE",
    "DEFAULT_PRICE_TABLE_AS_OF",
    "DEFAULT_STALENESS_WINDOW_DAYS",
    "ModelPrice",
    "PriceTable",
    "PricedCall",
    "StalenessAdvisory",
    "load_price_table",
    "price_table_staleness",
]
