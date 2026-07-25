"""Regression tests for issue #3013.

The run startup banner and the plan/approval box quoted a per-task cost
estimate and a model label that were hardcoded to Claude/sonnet regardless
of the model actually resolved for the run. A real e2e run resolving a
``:free`` route (e.g. ``nvidia/nemotron-3-nano-30b-a3b:free`` /
``openai/gpt-oss-20b:free``) whose real ``total_cost`` was ``$0.000000`` was
told the run would cost ``~$0.25-$0.75`` per task and labelled ``sonnet``.

Two invariants are pinned here, asserted against the *rendered* banner and
the *built* plan estimate (not just internal state):

* A resolved ``:free`` / non-Anthropic route shows a ``$0`` / "free route"
  estimate and the actual model id -- never a phantom sonnet rate.
* The free/zero decision is drawn from the *same* pricing source
  (:func:`price_model_usage` / ``MODEL_COSTS_PER_1M_TOKENS``) the run's
  actual ``total_cost`` aggregation uses, so the estimate and the final cost
  agree at $0.
* An Anthropic model still shows its real (non-zero) estimate and its label.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

from pathlib import Path

import pytest
from bernstein.core.models import Task
from bernstein.core.plan_approval import _estimate_task_cost, configure_plan_models

from bernstein.cli.run_preflight import (
    _emit_preflight_runtime_warnings,
    _estimate_run_preview,
    console,
)
from bernstein.core.cost.model_prices import is_free_route, price_model_usage

_FREE_MODEL = "openai/gpt-oss-20b:free"
_NEMOTRON_FREE = "nvidia/nemotron-3-nano-30b-a3b:free"

# Phantom figures a fixed sonnet/heuristic rate would have printed for a
# single unknown task (50k-150k tokens at the 0.005 fallback / 0.009 sonnet
# blended rate). None of these may appear for a free route.
_PHANTOM_FIGURES = ("$0.25", "$0.75", "$0.45", "$1.35", "$0.72")


def _render_banner(workdir: Path, model_override: str) -> tuple[object, str]:
    """Resolve the estimate for *model_override* and capture the banner text."""
    estimate = _estimate_run_preview(
        workdir=workdir,
        plan_file=None,
        goal=None,
        seed_file=None,
        model_override=model_override,
    )
    with console.capture() as cap:
        _emit_preflight_runtime_warnings(
            workdir=workdir,
            estimate=estimate,
            auto_approve=True,
            quiet=False,
        )
    return estimate, cap.get()


# ---------------------------------------------------------------------------
# is_free_route: the shared, table-anchored predicate
# ---------------------------------------------------------------------------


def test_is_free_route_detects_colon_free_suffix() -> None:
    assert is_free_route(_FREE_MODEL) is True
    assert is_free_route(_NEMOTRON_FREE) is True
    assert is_free_route("SomeVendor/Model-X:FREE") is True


def test_is_free_route_treats_unpriced_model_as_free() -> None:
    """An unknown model the metering path prices at $0 is a free route."""
    unknown = "totally-unknown-model-xyz"
    assert price_model_usage(unknown, 1000, 1000).priced is False
    assert is_free_route(unknown) is True


def test_is_free_route_false_for_priced_anthropic_models() -> None:
    assert is_free_route("sonnet") is False
    assert is_free_route("opus") is False


# ---------------------------------------------------------------------------
# Banner: free route -> $0 and the actual model id
# ---------------------------------------------------------------------------


def test_free_route_banner_is_zero_and_names_the_model(tmp_path: Path) -> None:
    estimate, out = _render_banner(tmp_path, _FREE_MODEL)

    # Internal state: zeroed and flagged free.
    assert estimate.free_route is True
    assert estimate.low_usd == 0.0
    assert estimate.high_usd == 0.0
    assert estimate.band is not None
    assert estimate.band.p50 == 0.0
    assert estimate.band.p90 == 0.0

    # Rendered banner: the actual model id, a $0 / free signal, and NO
    # phantom sonnet-rate figure.
    assert _FREE_MODEL in out, out
    assert "free route" in out.lower(), out
    assert "$0.00" in out, out
    for phantom in _PHANTOM_FIGURES:
        assert phantom not in out, f"phantom cost {phantom} leaked into banner: {out!r}"


def test_free_route_estimate_matches_total_cost_source(tmp_path: Path) -> None:
    """The banner $0 and the run's metered $0 come from the same table."""
    # The run's actual total_cost source meters this route at exactly $0.
    metered = price_model_usage(_FREE_MODEL, 120_000, 40_000)
    assert metered.cost_usd == 0.0
    assert metered.priced is False

    estimate, _out = _render_banner(tmp_path, _FREE_MODEL)
    assert estimate.high_usd == metered.cost_usd == 0.0


# ---------------------------------------------------------------------------
# Banner: Anthropic model still shows a real estimate and label
# ---------------------------------------------------------------------------


def test_anthropic_model_banner_shows_real_estimate_and_label(tmp_path: Path) -> None:
    estimate, out = _render_banner(tmp_path, "opus")

    assert estimate.free_route is False
    assert estimate.band is not None
    assert estimate.band.p90 > 0.0, out
    assert "opus" in out, out
    assert "free route" not in out.lower(), out


# ---------------------------------------------------------------------------
# Plan / approval box: role labelled with the resolved model, $0 for free
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_plan_models() -> object:
    """Ensure the module-global seed defaults never leak between tests."""
    configure_plan_models(None)
    yield
    configure_plan_models(None)


def _manager_task() -> Task:
    return Task(id="t1", title="do work", description="", role="manager")


def test_plan_box_free_route_zero_cost_and_resolved_label() -> None:
    """A seed whose only model is a :free route must not label/price sonnet."""
    configure_plan_models(None, default_model=_NEMOTRON_FREE)
    est = _estimate_task_cost(_manager_task())
    assert est.model == _NEMOTRON_FREE
    assert est.model != "sonnet"
    assert est.estimated_cost_usd == 0.0


def test_plan_box_uses_resolved_default_model_not_sonnet() -> None:
    """A non-free seed default model is labelled and priced, not sonnet."""
    configure_plan_models(None, default_model="opus")
    est = _estimate_task_cost(_manager_task())
    assert est.model == "opus"
    assert est.estimated_cost_usd > 0.0
