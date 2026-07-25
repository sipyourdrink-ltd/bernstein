"""Guard: every server path a CLI command calls is a registered route.

A CLI command that talks to a path the task server never registers fails
against a *running* server, which is worse than no command at all - the
operator cannot tell a missing feature from a broken install. This test
walks ``src/bernstein/cli/`` with the AST, collects the literal paths passed
to ``server_get`` / ``server_post``, and asserts each one matches a route on
the app that ``create_app()`` builds.

Both sides are normalised to a *shape*: query strings are dropped and every
path parameter (``{task_id}`` in a route, an f-string placeholder in a call
site) collapses to ``{}``. So ``server_get(f"/tasks/{task_id}/cancel")``
matches the registered ``/tasks/{task_id}/cancel``, and a call to a path with
no matching route - by name or by arity - fails.

Dynamic paths (a variable, a concatenation, a computed base) are skipped: the
AST cannot resolve them, and a false failure on an unresolvable expression
would push contributors to disable the guard.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from bernstein.core.server import create_app

_CLI_ROOT = Path(__file__).resolve().parents[2] / "src" / "bernstein" / "cli"
_SERVER_CALLS = frozenset({"server_get", "server_post"})
_PARAM_RE = re.compile(r"\{[^}]*\}")


def _normalise(path: str) -> str:
    """Return the comparable shape of *path*: no query string, ``{}`` params."""
    path = path.split("?", 1)[0].split("#", 1)[0]
    path = _PARAM_RE.sub("{}", path)
    return path.rstrip("/") or "/"


def _literal_path(node: ast.expr) -> str | None:
    """Return the path template for a call argument, or None if it is dynamic."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            elif isinstance(value, ast.FormattedValue):
                parts.append("{}")
            else:  # pragma: no cover - defensive
                return None
        return "".join(parts)
    return None


def _called_name(func: ast.expr) -> str | None:
    """Return the bare callee name for ``f(...)`` and ``mod.f(...)``."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _collect_cli_server_paths() -> dict[str, list[str]]:
    """Map each normalised server path to the CLI files that request it."""
    found: dict[str, list[str]] = {}
    for py_file in sorted(_CLI_ROOT.rglob("*.py")):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _called_name(node.func) not in _SERVER_CALLS:
                continue
            if not node.args:
                continue
            raw = _literal_path(node.args[0])
            if raw is None or not raw.startswith("/"):
                continue
            location = f"{py_file.relative_to(_CLI_ROOT.parents[2])}:{node.lineno}"
            found.setdefault(_normalise(raw), []).append(location)
    return found


def _registered_route_shapes() -> set[str]:
    """Return the normalised shape of every route the task server mounts."""
    app = create_app()
    return {_normalise(route.path) for route in app.routes if hasattr(route, "path")}


def test_cli_calls_only_registered_server_routes() -> None:
    """No CLI command may call a server path the app does not register."""
    cli_paths = _collect_cli_server_paths()
    assert cli_paths, "AST scan found no server_get/server_post call sites - the scan is broken"

    registered = _registered_route_shapes()
    missing = {path: sites for path, sites in cli_paths.items() if path not in registered}

    assert not missing, "CLI commands call server routes that are not registered:\n" + "\n".join(
        f"  {path}  <- {', '.join(sites)}" for path, sites in sorted(missing.items())
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/tasks?status=open", "/tasks"),
        ("/tasks/{task_id}/cancel", "/tasks/{}/cancel"),
        ("/tasks/{}", "/tasks/{}"),
        ("/status", "/status"),
        ("/", "/"),
    ],
)
def test_normalise_collapses_params_and_queries(raw: str, expected: str) -> None:
    """The shape comparison must ignore query strings and parameter names."""
    assert _normalise(raw) == expected


def test_scan_reads_fstring_call_sites() -> None:
    """f-string paths must be captured, not skipped as dynamic."""
    tree = ast.parse('server_get(f"/tasks/{task_id}/gates")')
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
    assert _normalise(_literal_path(call.args[0]) or "") == "/tasks/{}/gates"


def test_scan_skips_unresolvable_paths() -> None:
    """A computed path is skipped rather than reported as a bogus route."""
    tree = ast.parse('server_get(base + "/status")')
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
    assert _literal_path(call.args[0]) is None
