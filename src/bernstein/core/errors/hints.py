"""Rich-formatted hint renderer for first-run error categories.

Each :class:`ErrorCategory` maps to a short, context-aware hint shown in a
Rich :class:`~rich.panel.Panel` with a category-coloured border.  The hint
is one line of plain text plus optionally a fenced command block.

The renderer is pure: it returns a :class:`Panel` and does not write to
any console.  Callers wire it to a console via :func:`render_hint`.
"""

from __future__ import annotations

from typing import Any, Final, TypedDict

from rich.console import Console, Group
from rich.panel import Panel
from rich.text import Text
from rich.traceback import Traceback

from bernstein.core.errors.categories import ErrorCategory


class HintContext(TypedDict, total=False):
    """Optional context fields used when rendering hints.

    All fields are optional; the renderer falls back to neutral defaults
    when a field is absent.  Keep this struct flat to make it easy to
    populate from raw exception attributes.
    """

    adapter: str
    env_var: str
    provider: str
    package_manager_command: str
    timeout_seconds: int
    path: str
    port: int
    repo: str


_CATEGORY_BORDER: Final[dict[ErrorCategory, str]] = {
    ErrorCategory.CONFIG_MISSING: "yellow",
    ErrorCategory.AUTH_FAILED: "red",
    ErrorCategory.DEPENDENCY_MISSING: "magenta",
    ErrorCategory.MODEL_UNREACHABLE: "cyan",
    ErrorCategory.TIMEOUT: "orange3",
    ErrorCategory.PERMISSION_DENIED: "red",
    ErrorCategory.PORT_CONFLICT: "yellow",
    ErrorCategory.NO_PLAN_PRODUCED: "red",
    ErrorCategory.UNKNOWN: "white",
}


_CATEGORY_TITLE: Final[dict[ErrorCategory, str]] = {
    ErrorCategory.CONFIG_MISSING: "Configuration missing",
    ErrorCategory.AUTH_FAILED: "Authentication failed",
    ErrorCategory.DEPENDENCY_MISSING: "Dependency missing",
    ErrorCategory.MODEL_UNREACHABLE: "Model unreachable",
    ErrorCategory.TIMEOUT: "Timed out",
    ErrorCategory.PERMISSION_DENIED: "Permission denied",
    ErrorCategory.PORT_CONFLICT: "Port conflict",
    ErrorCategory.NO_PLAN_PRODUCED: "No plan produced",
    ErrorCategory.UNKNOWN: "Unhandled error",
}


def _ctx_get(context: HintContext | None, key: str, default: str = "") -> str:
    """Return a string field from a hint context, falling back to ``default``."""
    if context is None:
        return default
    value: Any = context.get(key)  # type: ignore[call-overload]
    if value is None or value == "":
        return default
    return str(value)


def _ctx_int(context: HintContext | None, key: str, default: int) -> int:
    """Return an int field from a hint context, falling back to ``default``."""
    if context is None:
        return default
    value: Any = context.get(key)  # type: ignore[call-overload]
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return default


def _hint_body(category: ErrorCategory, context: HintContext | None) -> tuple[str, str]:
    """Return ``(prose, command)`` for a category.

    ``command`` may be the empty string when no inline command is shown.

    Args:
        category: The error category.
        context: Optional hint context (adapter name, env var, etc.).

    Returns:
        Tuple of (one-line prose, optional command-line snippet).
    """
    if category is ErrorCategory.CONFIG_MISSING:
        return (
            "No bernstein config found.",
            "bernstein init",
        )
    if category is ErrorCategory.AUTH_FAILED:
        adapter = _ctx_get(context, "adapter", "the configured adapter")
        env_var = _ctx_get(context, "env_var")
        if env_var:
            return (
                f"Adapter `{adapter}` could not authenticate. Check `{env_var}` or run `{adapter} auth`.",
                f"export {env_var}=...",
            )
        return (
            f"Adapter `{adapter}` could not authenticate. Check the adapter's API key or run `{adapter} auth`.",
            f"{adapter} auth",
        )
    if category is ErrorCategory.DEPENDENCY_MISSING:
        adapter = _ctx_get(context, "adapter", "the configured adapter")
        pkg = _ctx_get(context, "package_manager_command")
        if pkg:
            return (
                f"Adapter `{adapter}` is configured but the binary is not installed. "
                f"Install it or remove the adapter from bernstein.yaml.",
                pkg,
            )
        return (
            f"Adapter `{adapter}` is configured but the binary is not installed. "
            f"Install it or remove the adapter from bernstein.yaml.",
            "",
        )
    if category is ErrorCategory.MODEL_UNREACHABLE:
        provider = _ctx_get(context, "provider", "the model provider")
        return (
            f"Could not reach `{provider}`. Check network or set `BERNSTEIN_OFFLINE=1` to use the mock adapter.",
            "BERNSTEIN_OFFLINE=1 bernstein run",
        )
    if category is ErrorCategory.TIMEOUT:
        adapter = _ctx_get(context, "adapter", "the adapter")
        seconds = _ctx_int(context, "timeout_seconds", 0)
        if seconds > 0:
            return (
                f"Adapter `{adapter}` timed out after {seconds}s. "
                f"Increase `timeout_seconds` in bernstein.yaml or check adapter health.",
                "",
            )
        return (
            f"Adapter `{adapter}` timed out. Increase `timeout_seconds` in bernstein.yaml or check adapter health.",
            "",
        )
    if category is ErrorCategory.PERMISSION_DENIED:
        path = _ctx_get(context, "path", "the target directory")
        return (
            f"Could not create worktree at `{path}`. "
            f"Check directory ownership and `git config --global init.defaultBranch`.",
            "",
        )
    if category is ErrorCategory.PORT_CONFLICT:
        port = _ctx_int(context, "port", 8052)
        return (
            f"Port `{port}` already in use. Set `BERNSTEIN_PORT` or stop the conflicting process.",
            f"lsof -i :{port}",
        )
    if category is ErrorCategory.NO_PLAN_PRODUCED:
        adapter = _ctx_get(context, "adapter", "the configured adapter")
        return (
            f"The spawned `{adapter}` agent exited before decomposing the goal, so no work "
            f"plan was produced. Check `.sdd/runtime/retrospective.md` for what the agent did "
            f"before it died.",
            "bernstein status",
        )
    # ErrorCategory.UNKNOWN
    repo = _ctx_get(context, "repo", "chernistry/bernstein")
    return (
        f"Unhandled error. Run with `--verbose` and file at https://github.com/{repo}/issues with the trace.",
        "bernstein run --verbose",
    )


def hint_for(category: ErrorCategory, context: HintContext | None = None) -> Panel:
    """Return a Rich :class:`Panel` rendering the hint for ``category``.

    The panel border colour is keyed off the category; the title is a
    short human label.  The body is a one-line prose hint, followed by an
    optional command snippet rendered as a dim block.

    Args:
        category: The error category.
        context: Optional hint context (adapter name, env var, etc.).

    Returns:
        A Rich :class:`Panel`.
    """
    prose, command = _hint_body(category, context)
    body = Text(prose)
    if command:
        body.append("\n\n")
        body.append(command, style="bold dim")
    return Panel(
        body,
        title=f"[bold]{_CATEGORY_TITLE[category]}[/bold]",
        title_align="left",
        border_style=_CATEGORY_BORDER[category],
        padding=(0, 1),
    )


def render_hint(
    console: Console,
    category: ErrorCategory,
    *,
    exc: BaseException | None = None,
    context: HintContext | None = None,
    verbose: bool = False,
) -> None:
    """Render a hint panel to ``console``; show the traceback when verbose.

    Args:
        console: Target Rich console (typically stderr).
        category: The error category.
        exc: Optional exception. When provided and ``verbose`` is True,
            the formatted traceback is printed below the hint.
        context: Optional hint context (adapter name, env var, etc.).
        verbose: When True, also render the traceback for ``exc`` below
            the hint.  When False (the default), the traceback is hidden.
    """
    panel = hint_for(category, context)
    if verbose and exc is not None:
        tb = Traceback.from_exception(
            type(exc),
            exc,
            exc.__traceback__,
            show_locals=False,
        )
        console.print(Group(panel, tb))
        return
    console.print(panel)
