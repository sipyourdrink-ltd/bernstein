"""Global --verbose / --quiet flag support for all CLI commands.

--verbose and --quiet flags.

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


#: Every ``bernstein.*`` module logger (``logging.getLogger(__name__)``) is a
#: descendant of this one, so setting its level and handler is enough to
#: cover the whole package without ever touching the *root* logger -- which
#: every other logger in the process, including third-party libraries and
#: (in a pytest worker) every later test's own loggers, also inherits from
#: by default (#6184).
_BERNSTEIN_LOGGER_NAME = "bernstein"


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

    # Scope reconfiguration to bernstein's own logger tree, never the
    # process root. `--verbose`/`--quiet` mean "change how much of this
    # program's own output the user sees", not "change what every logger in
    # the process does" -- the previous `logging.basicConfig(..., force=True)`
    # reconfigured the *root* logger, which is process-wide and permanent:
    # in a long-running process it silently lowers third-party loggers too,
    # and in a pytest worker that runs a CLI command through the Click
    # layer, it leaves every later test in that worker with a raised root
    # level, so a `caplog` assertion on an unrelated logger reads "nothing
    # was logged" instead of "the level was raised" (#6184).
    if verbose:
        _configure_bernstein_logger(logging.DEBUG, "%(levelname)s %(name)s: %(message)s")
    elif quiet:
        _configure_bernstein_logger(logging.ERROR, "%(message)s")


def _configure_bernstein_logger(level: int, fmt: str) -> None:
    """Set the bernstein logger's level and its own formatted handler.

    ``propagate = False`` keeps the record from also reaching any handler a
    caller (or `setup_json_logging`) has attached to root, which would
    otherwise print every message twice under a different format.
    Replacing rather than appending the handler list keeps a second
    `--verbose`/`--quiet` invocation in the same process (Click re-invoking
    a command under test, or successive CLI calls in one script) from
    stacking duplicate handlers.
    """
    logger = logging.getLogger(_BERNSTEIN_LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(fmt))
    logger.handlers = [handler]


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
