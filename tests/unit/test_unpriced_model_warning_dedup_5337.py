"""Regression tests for issue #5337.

Two defects on the unpriced-model path:

* ``price_model_usage`` logged the full "no pricing-table entry" warning on
  *every* call for a gateway/alias route with no table entry. In a
  non-interactive run the line repeats for every call and every cost
  estimate and buries the output an operator needs. The warning must fire
  once per distinct model name per process; later calls meter at $0
  silently, ``priced`` stays ``False`` and totals still carry the tokens.
* Cost-estimate lines quoted ``$0.00`` for such a name as if the run were
  free. They must read ``unpriced`` instead. A ``:free`` id and a priced
  model are unaffected.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.cost.model_prices import (
    _reset_unpriced_model_warnings,
    model_cost_is_known,
    model_has_pricing_entry,
    price_model_usage,
)


@pytest.fixture(autouse=True)
def _clear_warning_cache() -> object:
    """The once-per-process cache is module-global; isolate each test."""
    _reset_unpriced_model_warnings()
    yield
    _reset_unpriced_model_warnings()


# ---------------------------------------------------------------------------
# price_model_usage: warn once per name per process
# ---------------------------------------------------------------------------


def test_unpriced_model_warns_only_once_for_the_same_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING"):
        first = price_model_usage("fleet-live", 1, 1)
        second = price_model_usage("fleet-live", 500, 250)

    records = [r for r in caplog.records if "no pricing-table entry" in r.message]
    assert len(records) == 1

    # Metering itself is unchanged on every call.
    for result in (first, second):
        assert result.priced is False
        assert result.cost_usd == 0.0
    assert second.input_tokens == 500
    assert second.output_tokens == 250


def test_two_distinct_unpriced_names_each_warn_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING"):
        price_model_usage("fleet-live", 1, 1)
        price_model_usage("fleet-live", 1, 1)
        price_model_usage("lb-alias-eu", 1, 1)
        price_model_usage("lb-alias-eu", 1, 1)

    warned = sorted(r.args[0] for r in caplog.records if "no pricing-table entry" in r.message)
    assert warned == ["fleet-live", "lb-alias-eu"]


def test_priced_model_never_warns(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING"):
        result = price_model_usage("gpt-5-mini", 1_000, 1_000)

    assert result.priced is True
    assert not [r for r in caplog.records if "no pricing-table entry" in r.message]


# ---------------------------------------------------------------------------
# The predicates the display code keys on
# ---------------------------------------------------------------------------


def test_model_has_pricing_entry_matches_price_model_usage() -> None:
    assert model_has_pricing_entry("gpt-5-mini") is True
    assert model_has_pricing_entry("claude-sonnet-5") is True
    assert model_has_pricing_entry("fleet-live") is False
    assert model_has_pricing_entry("openai/gpt-oss-20b:free") is False


def test_model_cost_is_known_treats_free_id_as_known_zero() -> None:
    # A ``:free`` id has a real price ($0); an alias with no entry does not.
    assert model_cost_is_known("openai/gpt-oss-20b:free") is True
    assert model_cost_is_known("SomeVendor/Model-X:FREE") is True
    assert model_cost_is_known("gpt-5-mini") is True
    assert model_cost_is_known("fleet-live") is False


# ---------------------------------------------------------------------------
# Cost-estimate lines: "unpriced", not "$0.00"
# ---------------------------------------------------------------------------


def test_describe_cost_estimate_says_unpriced_for_no_entry_model() -> None:
    from bernstein.core.orchestration.bootstrap import _describe_cost_estimate

    known_count = _describe_cost_estimate(3, "fleet-live")
    assert "unpriced" in known_count
    assert "$0.00" not in known_count

    pending = _describe_cost_estimate(0, "fleet-live")
    assert "unpriced" in pending
    assert "$0.00" not in pending


def test_describe_cost_estimate_unaffected_for_priced_model() -> None:
    from bernstein.core.orchestration.bootstrap import _describe_cost_estimate

    line = _describe_cost_estimate(3, "sonnet")
    assert "unpriced" not in line
    assert line.startswith("~$")


def _render_preflight_banner(workdir: Path, model_override: str) -> str:
    from bernstein.cli.run_preflight import (
        _emit_preflight_runtime_warnings,
        _estimate_run_preview,
        console,
    )

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
    return cap.get()


def test_preflight_banner_reads_unpriced_for_no_entry_model(tmp_path: Path) -> None:
    out = _render_preflight_banner(tmp_path, "fleet-live")
    assert "unpriced" in out.lower()
    assert "$0.00" not in out
    assert "free route" not in out.lower()
    assert "fleet-live" in out


def test_preflight_banner_still_zero_for_colon_free_route(tmp_path: Path) -> None:
    # Issue #3013 behaviour is untouched: a real :free route is $0, not "unpriced".
    out = _render_preflight_banner(tmp_path, "openai/gpt-oss-20b:free")
    assert "free route" in out.lower()
    assert "$0.00" in out
    assert "unpriced" not in out.lower()
