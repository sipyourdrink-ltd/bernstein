"""First-run guard helpers wiring categorisation into CLI entry points.

This module provides a single function, :func:`handle_first_run_exception`,
that maps a raw exception from a top-level Click command body into:

1. A structured :class:`bernstein.core.errors.ErrorCategory`.
2. A Rich-formatted hint panel rendered to stderr.
3. A ``SystemExit`` carrying the sysexits.h exit code for that category.

The verbose flag retains the original traceback after the hint panel;
the default mode hides it.

Callers wrap the command body::

    @click.command()
    @click.pass_context
    def my_cmd(ctx: click.Context, ...) -> None:
        try:
            _impl()
        except BaseException as exc:
            handle_first_run_exception(exc, verbose=ctx.obj.get("VERBOSE", False))
"""

from __future__ import annotations

from typing import NoReturn

import click
from rich.console import Console

from bernstein.core.errors import (
    BernsteinFirstRunError,
    ErrorCategory,
    HintContext,
    categorize_exception,
    exit_code_for,
    render_hint,
)

_stderr_console = Console(stderr=True)


def _context_from_exception(exc: BaseException) -> HintContext:
    """Best-effort extraction of hint context from an exception's attributes.

    The categorisation taxonomy is closed, but the hint renderer benefits
    from any extra context the exception happens to carry (adapter name,
    env var, port number, file path).
    """
    ctx: HintContext = {}
    adapter = getattr(exc, "adapter", None)
    if isinstance(adapter, str) and adapter:
        ctx["adapter"] = adapter
    env_var = getattr(exc, "env_var", None)
    if isinstance(env_var, str) and env_var:
        ctx["env_var"] = env_var
    install_hint = getattr(exc, "install_hint", None)
    if isinstance(install_hint, str) and install_hint:
        ctx["package_manager_command"] = install_hint
    filename = getattr(exc, "filename", None)
    if isinstance(filename, str) and filename:
        ctx["path"] = filename
    if isinstance(exc, BernsteinFirstRunError):
        # The typed first-run error already carries an authoritative ctx.
        for key, value in exc.context.items():
            if value is None or value == "":
                continue
            ctx[key] = value  # type: ignore[literal-required]
    return ctx


def _capture_to_telemetry_sink(exc: BaseException) -> None:
    """Forward ``exc`` to the operator-managed error sink, if one is wired.

    This is the explicit-capture counterpart to ``sys.excepthook``: the
    default hook never fires for exceptions caught by the CLI's
    first-run barrier, so without this call no event reaches the error
    sink even though the SDK is initialised. The function is a no-op when:

    * ``sentry-sdk`` is not installed (minimal install without the
      ``observability`` extra), or
    * the SDK was never initialised (operator has not exported a DSN).

    Telemetry is best-effort and must not crash the CLI's exit path:
    any error raised by the SDK is swallowed.
    """
    try:
        import sentry_sdk  # type: ignore[import-not-found]
    except ImportError:
        return
    try:
        client = sentry_sdk.get_client()
        if not client.is_active():
            return
        sentry_sdk.capture_exception(exc)
    except Exception:
        # Best-effort: a misbehaving SDK must never block the exit path.
        return


def handle_first_run_exception(
    exc: BaseException,
    *,
    verbose: bool = False,
    console: Console | None = None,
) -> NoReturn:
    """Render a hint and raise :class:`SystemExit` with the category's exit code.

    Click's command runner catches :class:`SystemExit` and propagates it
    through its standard exit path, so callers can use this from inside a
    command body without disturbing Click's traceback behaviour.

    Args:
        exc: The raw exception caught at the top of a Click command body.
        verbose: When True, the original traceback is rendered below the
            hint panel.  When False, the traceback is hidden.
        console: Optional Rich console (defaults to a stderr console).

    Raises:
        SystemExit: Always, with ``code`` set to the sysexits.h exit code
            for the category derived from ``exc``.
    """
    # ``click.UsageError`` already has Click's own pretty handler; defer
    # to it so option/argument errors keep their familiar shape.
    if isinstance(exc, click.UsageError):
        raise exc
    # Honour an explicit ``SystemExit`` raised by inner code; do not
    # overwrite the operator's chosen exit code with a categorised one.
    if isinstance(exc, SystemExit):
        raise exc

    # Forward to the Sentry-compatible sink before the pretty
    # print. The default ``sys.excepthook`` never sees this exception
    # because we are about to convert it into a SystemExit, so explicit
    # capture is required to keep the error sink in the loop.
    _capture_to_telemetry_sink(exc)

    category = categorize_exception(exc)
    ctx = _context_from_exception(exc)
    target = console or _stderr_console
    render_hint(target, category, exc=exc, context=ctx, verbose=verbose)
    raise SystemExit(exit_code_for(category))


def category_for(exc: BaseException) -> ErrorCategory:
    """Re-export of :func:`bernstein.core.errors.categorize_exception`.

    Provided so call sites in ``cli/`` can ``from .first_run_guard import``
    a single symbol when they only need the structured category.
    """
    return categorize_exception(exc)
