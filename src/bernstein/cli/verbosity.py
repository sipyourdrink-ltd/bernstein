"""Global --verbose / --quiet flag support for all CLI commands.

CLI-005: --verbose and --quiet flags.

Provides a Click callback and context helpers that configure Python
logging levels based on the flags.  ``--verbose`` sets DEBUG level;
``--quiet`` suppresses everything below ERROR.
"""

from __future__ import annotations

import logging
from typing import Any

import click

# ---------------------------------------------------------------------------
# Context key for verbosity state
# ---------------------------------------------------------------------------

_VERBOSITY_KEY = "bernstein_verbosity"

# Verbosity levels: -1 = quiet, 0 = normal, 1 = verbose
QUIET = -1
NORMAL = 0
VERBOSE = 1


def get_verbosity() -> int:
    """Return the current verbosity level from the Click context.

    Returns:
        -1 for quiet, 0 for normal, 1 for verbose.
    """
    ctx = click.get_current_context(silent=True)
    if ctx and ctx.obj:
        return int(ctx.obj.get(_VERBOSITY_KEY, NORMAL))
    return NORMAL


def is_verbose() -> bool:
    """Return True if --verbose was passed."""
    return get_verbosity() >= VERBOSE


def is_quiet() -> bool:
    """Return True if --quiet was passed."""
    return get_verbosity() <= QUIET


def apply_verbosity(verbose: bool, quiet: bool) -> None:
    """Apply verbosity settings to the Click context and Python logging.

    Args:
        verbose: True if --verbose was passed.
        quiet: True if --quiet was passed.
    """
    ctx = click.get_current_context(silent=True)
    if ctx:
        ctx.ensure_object(dict)
        if verbose:
            ctx.obj[_VERBOSITY_KEY] = VERBOSE
        elif quiet:
            ctx.obj[_VERBOSITY_KEY] = QUIET
        else:
            ctx.obj[_VERBOSITY_KEY] = NORMAL

    # Configure root logger
    if verbose:
        logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s: %(message)s", force=True)
    elif quiet:
        logging.basicConfig(level=logging.ERROR, format="%(message)s", force=True)


def verbose_option(fn: Any) -> Any:
    """Click decorator adding --verbose and --quiet flags to a command.

    Usage::

        @click.command()
        @verbose_option
        def my_command(**kwargs: Any) -> None:
            if is_verbose():
                click.echo("Debug info...")
    """
    import functools

    @click.option("--verbose", "-v", is_flag=True, default=False, help="Show debug-level output.")
    @click.option("--quiet", "-q", is_flag=True, default=False, help="Suppress all non-error output.")
    @functools.wraps(fn)
    def wrapper(*args: Any, verbose: bool = False, quiet: bool = False, **kwargs: Any) -> Any:
        if verbose and quiet:
            raise click.UsageError("Cannot use --verbose and --quiet together.")
        apply_verbosity(verbose, quiet)
        return fn(*args, **kwargs)

    return wrapper
