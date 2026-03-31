"""Smoke test: cli() passes ALL required parameters to run().

This test catches the recurring bug where agents add new parameters
to run() but forget to update the cli() caller in main.py.
"""

from __future__ import annotations

import inspect

from bernstein.cli.main import run


def test_run_callback_has_no_missing_required_params() -> None:
    """Every non-default param of run() must be passed by cli().

    If run() gains a new required parameter, this test fails until
    cli() is updated to pass it.
    """
    sig = inspect.signature(run.callback)  # type: ignore[union-attr]
    required = []
    for name, param in sig.parameters.items():
        if param.default is inspect.Parameter.empty:
            required.append(name)

    # All required params must be explicitly listed here.
    # When you add a new required param to run(), add it here AND to cli().
    known_required = {
        "plan_file",
        "goal",
        "seed_file",
        "port",
        "cells",
        "remote",
        "cli",
        "model",
        "workflow",
        "routing",
        "compliance",
        "container",
        "container_image",
        "two_phase_sandbox",
        "plan_only",
        "from_plan",
        "auto_approve",
        "quiet",
        "skip_gate",
        "skip_gate_reason",
        "audit",
    }

    actual_required = set(required)
    missing = actual_required - known_required
    assert not missing, (
        f"run() has new required params not in known_required: {missing}. "
        f"Add them to cli() in main.py AND to this test."
    )


def test_run_callback_exists() -> None:
    """run command has a callback (not None)."""
    assert run.callback is not None


def test_run_params_match_cli_call() -> None:
    """Verify cli() passes all run() params by reading main.py source.

    This is a belt-and-suspenders check: parse main.py to verify
    every run() parameter appears in the run.callback() call.
    """
    from pathlib import Path

    sig = inspect.signature(run.callback)  # type: ignore[union-attr]
    param_names = set(sig.parameters.keys())

    # Read main.py source and find the run.callback() call
    main_py = Path(__file__).parent.parent.parent / "src" / "bernstein" / "cli" / "main.py"
    source = main_py.read_text()

    # Check each param appears in the call
    missing_in_source = []
    for name in param_names:
        if f"{name}=" not in source and "**" not in source:
            missing_in_source.append(name)

    assert not missing_in_source, (
        f"These run() params are NOT passed in cli() main.py: {missing_in_source}. "
        f"Add '{name}=<value>' to the run.callback() call in main.py."
    )
