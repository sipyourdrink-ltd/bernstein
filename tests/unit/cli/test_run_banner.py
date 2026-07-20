"""Regression test for the startup banner on ``bernstein run``.

History: commit 1e5c13013 removed the ``print_banner()`` call from the
``run``/``conduct`` callback with the (incorrect) note that the parent
``cli()`` group already printed it.  The parent ``cli()`` callback in fact
early-returns when a subcommand is invoked, so the banner was silently
dropped for every ``bernstein run`` invocation.

These tests pin the restored behaviour so a future stub returning empty
output fails the gate.
"""

from __future__ import annotations

import contextlib
import io
import os
from contextlib import redirect_stdout
from typing import Any
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from bernstein.cli.helpers import BANNER, console, print_banner
from bernstein.cli.main import cli

# ---------------------------------------------------------------------------
# Direct print_banner contract
# ---------------------------------------------------------------------------


def test_print_banner_emits_non_empty_output(capsys: pytest.CaptureFixture[str]) -> None:
    """``print_banner()`` must emit a non-trivial banner.

    Guards against an accidental stub regression that returns ``""`` or
    suppresses the banner entirely.
    """
    # Force the shared Rich console to write to a captured buffer so we get
    # deterministic stdout regardless of the test environment's TTY state.
    buf = io.StringIO()
    with redirect_stdout(buf):
        with console.capture() as captured:
            print_banner()
    output = captured.get()

    # Banner must contain the recognisable subtitle so a future change
    # cannot replace it with an empty string.
    assert "Bernstein" in output, output
    assert "Agent Orchestra" in output, output

    # Hard byte-count floor: the smallest reasonable banner is ~3 lines and
    # well over 50 bytes once unicode + ANSI styling is rendered.  A stub
    # returning the empty string trips this gate.
    assert len(output) >= 50, f"banner unexpectedly short: {len(output)} bytes"


def test_banner_constant_contains_brand_strings() -> None:
    """``BANNER`` must keep the brand + subtitle so doctor/help look right."""
    assert "Bernstein" in BANNER
    assert "Agent Orchestra" in BANNER


# ---------------------------------------------------------------------------
# ``bernstein run`` CLI contract
# ---------------------------------------------------------------------------


def _invoke_run_plan_only(tmp_path: Any) -> str:
    """Invoke ``bernstein run --plan-only`` against a minimal seed.

    Heavy preflight (cost estimation, plan rendering) is short-circuited by
    raising as soon as the banner phase has completed; ``--plan-only`` exits
    before any network work is attempted but we still skip provider lookups
    by pointing at an empty seed.
    """
    seed = tmp_path / "bernstein.yaml"
    seed.write_text("goal: banner-regression-test\ntasks: []\n")

    runner = CliRunner()
    # ``invoke`` runs the Click command with the working directory inherited
    # from pytest, so chdir into the tmp path to keep the seed discovery
    # local + deterministic.
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        result = runner.invoke(cli, ["run", "--plan-only", str(seed)], catch_exceptions=False)
    finally:
        os.chdir(cwd)
    # ``--plan-only`` may exit non-zero on environments missing optional
    # providers; we only care that the banner ran first, so the exit code
    # itself is not asserted here.
    return result.output or ""


def test_run_subcommand_prints_banner(tmp_path: Any) -> None:
    """``bernstein run`` prints the banner before any plan output.

    Pins commit 1e5c13013 not to silently re-regress: the run callback must
    print the banner whenever the parent ``cli()`` group did not already
    show the premium splash.
    """
    output = _invoke_run_plan_only(tmp_path)
    assert "Bernstein" in output, output
    assert "Agent Orchestra" in output, output


def test_run_subcommand_skips_banner_when_splash_already_printed(tmp_path: Any) -> None:
    """When the parent ``cli()`` already printed the splash, ``run`` must not double-print.

    The splash path in ``main.cli`` sets ``ctx.obj["_BANNER_PRINTED"]``; the
    inner ``run`` callback honours that flag so ``bernstein`` (bare invocation,
    which prints the premium splash and then chains into ``run``) does not
    emit two banners back-to-back.
    """
    seed = tmp_path / "bernstein.yaml"
    seed.write_text("goal: banner-skip-test\ntasks: []\n")

    runner = CliRunner()
    cwd = os.getcwd()
    os.chdir(tmp_path)

    # Patch ``print_banner`` inside run_bootstrap so we can assert it was
    # NOT called when the splash-printed flag is set.  We invoke the inner
    # callback directly with the flag already populated on the Click
    # context.
    import click

    from bernstein.cli import run_bootstrap

    try:
        with patch.object(run_bootstrap, "print_banner") as banner_spy:
            # Build a fresh Click context with the splash-printed flag set,
            # then dispatch the ``run`` callback under it.
            ctx = click.Context(run_bootstrap.run)
            ctx.ensure_object(dict)
            ctx.obj["_BANNER_PRINTED"] = True
            with ctx, contextlib.suppress(SystemExit):
                # We don't actually need the run to succeed -- only to reach
                # the banner gate.  Any exception after that point is fine.
                runner.invoke(
                    cli,
                    ["run", "--plan-only", str(seed)],
                    catch_exceptions=False,
                    obj={"_BANNER_PRINTED": True},
                )
            # When the flag is set, banner must NOT be printed.
            assert banner_spy.call_count == 0, "banner was double-printed despite splash flag"
    finally:
        os.chdir(cwd)


# ---------------------------------------------------------------------------
# Bare ``bernstein`` (no subcommand) contract when the splash is disabled
# ---------------------------------------------------------------------------


def _invoke_bare_plan_only(tmp_path: Any) -> str:
    """Invoke the bare ``bernstein --plan-only`` entry point (no subcommand).

    This is the ``ctx.invoked_subcommand is None`` branch of ``main.cli`` --
    the "general program load" path that shows the startup splash and then
    chains into the run callback.  ``--plan-only`` exits before any agent is
    spawned.
    """
    seed = tmp_path / "bernstein.yaml"
    seed.write_text("goal: banner-bare-regression\ntasks: []\n")

    runner = CliRunner()
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        result = runner.invoke(cli, ["--plan-only"], catch_exceptions=False)
    finally:
        os.chdir(cwd)
    return result.output or ""


def test_bare_invocation_prints_banner_when_splash_disabled(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Bare ``bernstein`` with the splash disabled must still print the box banner.

    Regression: ``main.cli`` set ``ctx.obj["_BANNER_PRINTED"] = True``
    unconditionally right after calling ``splash()``.  When the splash was
    disabled (``BERNSTEIN_NO_SPLASH`` / ``visual.splash=false``) it rendered
    nothing, yet the flag still suppressed the inner run callback's box
    banner -- so the bare invocation showed *no* startup banner at all while
    ``bernstein run`` still did.  ``splash()`` now reports whether it emitted
    a banner and ``main.cli`` only sets the flag when it did.
    """
    monkeypatch.setenv("BERNSTEIN_NO_SPLASH", "1")
    output = _invoke_bare_plan_only(tmp_path)
    assert "Bernstein" in output, output
    assert "Agent Orchestra" in output, output


def test_bare_invocation_does_not_double_print_when_splash_enabled(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the splash enabled, the bare path must not *also* print the box banner.

    Pins the other side of the fix: when the splash actually renders (here the
    compact fallback, since CliRunner's stdout is not a TTY), the durable box
    banner (``BANNER`` / "Agent Orchestra") must stay suppressed so the operator
    never sees two banners stacked.
    """
    monkeypatch.delenv("BERNSTEIN_NO_SPLASH", raising=False)
    monkeypatch.delenv("CI", raising=False)
    output = _invoke_bare_plan_only(tmp_path)
    # The compact splash marker is present ...
    assert "declarative agent orchestration" in output, output
    # ... but the box banner's title-case subtitle must NOT be (no double-print).
    assert "Agent Orchestra" not in output, output


# ---------------------------------------------------------------------------
# ``splash()`` return-value contract (the mechanism main.cli relies on)
# ---------------------------------------------------------------------------


def test_splash_reports_false_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """``splash()`` returns ``False`` when disabled so callers can fall back.

    The bare ``cli()`` path branches on this value; if the splash ever again
    returns ``None``/truthy while rendering nothing, the box-banner fallback
    silently breaks.  This locks the contract at the unit level.
    """
    from rich.console import Console

    from bernstein.cli.display.splash_screen import splash as splash_fn

    monkeypatch.setenv("BERNSTEIN_NO_SPLASH", "1")
    con = Console(file=io.StringIO(), force_terminal=True)
    assert splash_fn(con, version="1.0", agents=[], skip_animation=True) is False


def test_splash_reports_true_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """``splash()`` returns ``True`` when it actually emits a banner."""
    from rich.console import Console

    from bernstein.cli.display.splash_screen import splash as splash_fn

    monkeypatch.delenv("BERNSTEIN_NO_SPLASH", raising=False)
    monkeypatch.delenv("CI", raising=False)
    con = Console(file=io.StringIO(), force_terminal=True)
    assert splash_fn(con, version="1.0", agents=[], skip_animation=True) is True
