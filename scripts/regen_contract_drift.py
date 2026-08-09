"""Regenerate contract allow-lists / forwards when CI flags drift.

Three contract tests in ``tests/unit/`` act as drift detectors against the
shape of the public CLI / API / ``cli()`` callback. They fail loud when
something new is added without updating a small allow-list. The fix in each
case is a 1-2 line edit to a data structure -- this script does that edit
automatically so the bot can open a follow-up PR.

Fixtures handled
----------------
* ``DOCUMENTED_COMMANDS``  -- ``tests/unit/test_readme_api_coverage.py``
* ``_INFRASTRUCTURE_PATHS`` -- ``tests/unit/test_api_v1_routing.py``
* ``cli_run_callback``     -- forward-arg list in ``src/bernstein/cli/main.py``

Usage
-----
::

    python scripts/regen_contract_drift.py --fixture DOCUMENTED_COMMANDS
    python scripts/regen_contract_drift.py --fixture _INFRASTRUCTURE_PATHS
    python scripts/regen_contract_drift.py --fixture cli_run_callback
    python scripts/regen_contract_drift.py --fixture all

The script writes back to disk in-place. Run ``git diff`` afterwards to see
what changed. Refuses to write if the resulting diff exceeds 30 LOC --
larger drift is a real signal and deserves a human.

References #1273.
"""

from __future__ import annotations

import argparse
import inspect
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Hard ceiling: drift fixes should be tiny. Anything bigger and we bail so a
# human looks at the diff (the bot would mis-classify a real semantic change
# as drift).
MAX_LOC_DELTA = 30


# ---------------------------------------------------------------------------
# Common helpers
# ---------------------------------------------------------------------------


def _diff_loc(before: str, after: str) -> int:
    """Return the number of changed lines (added + removed)."""
    before_lines = before.splitlines()
    after_lines = after.splitlines()
    # Cheap symmetric-diff line-count -- not a real diff algorithm but fine for
    # the rough size gate (we only care about order-of-magnitude).
    added = [ln for ln in after_lines if ln not in before_lines]
    removed = [ln for ln in before_lines if ln not in after_lines]
    return len(added) + len(removed)


def _write_if_changed(path: Path, before: str, after: str) -> bool:
    if before == after:
        print(f"[regen] {path.name}: no changes")
        return False
    delta = _diff_loc(before, after)
    if delta > MAX_LOC_DELTA:
        print(
            f"[regen] {path.name}: refusing to write, diff is {delta} LOC "
            f"(cap {MAX_LOC_DELTA}). Real change suspected -- open an issue.",
            file=sys.stderr,
        )
        return False
    path.write_text(after)
    print(f"[regen] {path.name}: wrote {delta} LOC of changes")
    return True


# ---------------------------------------------------------------------------
# Fixture 1: DOCUMENTED_COMMANDS
# ---------------------------------------------------------------------------


def regen_documented_commands() -> bool:
    """Add newly-registered CLI commands to UNDOCUMENTED_EXEMPTIONS allow-list."""
    from bernstein.cli.main import cli

    target = REPO_ROOT / "tests" / "unit" / "test_readme_api_coverage.py"
    source = target.read_text()

    import ast

    tree = ast.parse(source)
    exemptions: set[str] = set()
    for node in ast.walk(tree):
        target_name: str | None = None
        rhs: ast.AST | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target_name = node.targets[0].id
            rhs = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target_name = node.target.id
            rhs = node.value
        if target_name == "UNDOCUMENTED_EXEMPTIONS" and rhs is not None:
            for sub in ast.walk(rhs):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    exemptions.add(sub.value)
            break

    # Parse docs/reference/cli-reference.md for documented commands
    ref_path = REPO_ROOT / "docs" / "reference" / "cli-reference.md"
    import re

    ref_text = ref_path.read_text(encoding="utf-8") if ref_path.exists() else ""
    headings = re.findall(r"^#+\s+`bernstein\s+([a-zA-Z0-9_-]+)", ref_text, re.M)
    tables = re.findall(r"\|\s*`bernstein\s+([a-zA-Z0-9_-]+)", ref_text, re.M)
    documented = set(headings + tables)

    registered = set(cli.commands.keys())
    missing = sorted(registered - (documented | exemptions))
    if not missing:
        print("[regen] UNDOCUMENTED_EXEMPTIONS: nothing to add")
        return False

    anchor = "\n}\n\n# Backwards compatibility alias"
    if anchor not in source:
        print("[regen] UNDOCUMENTED_EXEMPTIONS: could not find anchor -- skipping", file=sys.stderr)
        return False

    bot_block_lines = []
    for name in missing:
        bot_block_lines.append(f'    "{name}": "Bot-added exemption (regen_contract_drift.py)",')
    bot_block = "\n".join(bot_block_lines) + "\n"

    pre, _, post = source.partition(anchor)
    new_source = pre + bot_block + anchor + post

    return _write_if_changed(target, source, new_source)


# ---------------------------------------------------------------------------
# Fixture 2: _INFRASTRUCTURE_PATHS
# ---------------------------------------------------------------------------


def regen_infrastructure_paths() -> bool:
    """Add new root-only FastAPI routes to the parity allow-list."""
    from fastapi.routing import APIRoute, APIWebSocketRoute
    from starlette.routing import WebSocketRoute

    from bernstein.core.server import create_app

    target = REPO_ROOT / "tests" / "unit" / "test_api_v1_routing.py"
    source = target.read_text()

    # Mirror the test's own path-collection logic so we know exactly which
    # paths it would flag.
    with tempfile.TemporaryDirectory() as tmpdir:
        jsonl_path = Path(tmpdir) / "tasks.jsonl"
        app = create_app(jsonl_path=jsonl_path)
        root_paths: set[str] = set()
        v1_relative: set[str] = set()
        for route in app.routes:
            if not isinstance(route, APIRoute | APIWebSocketRoute | WebSocketRoute):
                continue
            path = route.path
            if path.startswith("/api/v1/"):
                v1_relative.add(path[len("/api/v1") :])
            elif path == "/api/v1":
                continue
            else:
                root_paths.add(path)

    # Extract the existing _INFRASTRUCTURE_PATHS allow-list via AST.
    import ast

    tree = ast.parse(source)
    documented: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "_INFRASTRUCTURE_PATHS"
        ):
            for sub in ast.walk(node.value or ast.Constant(None)):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    documented.add(sub.value)
            break

    # A path qualifies as drift if it's root-mounted, has no /api/v1 mirror,
    # and isn't already in _INFRASTRUCTURE_PATHS. The test treats such paths
    # as failures.
    missing_in_v1 = {p for p in root_paths if p not in documented and p not in v1_relative}
    if not missing_in_v1:
        print("[regen] _INFRASTRUCTURE_PATHS: nothing to add")
        return False

    # Same insertion strategy as DOCUMENTED_COMMANDS: anchor to the next
    # top-level construct (an ``@pytest.fixture`` line in this file) and walk
    # back to the closing brace.
    anchor = "\n@pytest.fixture()"
    if anchor not in source:
        print("[regen] _INFRASTRUCTURE_PATHS: could not find anchor -- skipping", file=sys.stderr)
        return False
    pre, _, _ = source.partition(anchor)
    close_idx = pre.rfind("    }\n)")
    if close_idx == -1:
        print(
            "[regen] _INFRASTRUCTURE_PATHS: could not locate frozenset close -- skipping",
            file=sys.stderr,
        )
        return False

    bot_block_lines = ["        # Bot-added: drift autofix (regen_contract_drift.py)"]
    for name in sorted(missing_in_v1):
        bot_block_lines.append(f'        "{name}",')
    bot_block = "\n".join(bot_block_lines) + "\n"

    new_source = pre[:close_idx] + bot_block + pre[close_idx:] + source[len(pre) :]
    return _write_if_changed(target, source, new_source)


# ---------------------------------------------------------------------------
# Fixture 3: cli() forward-arg list
# ---------------------------------------------------------------------------


def regen_cli_run_callback() -> bool:
    """Add any new run() params as forwarded kwargs in the cli() callback."""
    from bernstein.cli.main import run

    callback = run.callback
    assert callback is not None, "run command has no callback"
    sig = inspect.signature(callback)
    run_params = list(sig.parameters.keys())

    target = REPO_ROOT / "src" / "bernstein" / "cli" / "main.py"
    source = target.read_text()

    # Locate the ``run.callback(`` call inside ``def cli(...)`` and find the
    # kwargs that are already forwarded.
    import ast

    tree = ast.parse(source)
    call_node: ast.Call | None = None
    func_def_node: ast.FunctionDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "cli":
            func_def_node = node
            for sub in ast.walk(node):
                if (
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr == "callback"
                    and isinstance(sub.func.value, ast.Name)
                    and sub.func.value.id == "run"
                ):
                    call_node = sub
                    break
            break

    if call_node is None or func_def_node is None:
        print("[regen] cli_run_callback: could not locate run.callback() call", file=sys.stderr)
        return False

    forwarded = {kw.arg for kw in call_node.keywords if kw.arg is not None}
    cli_params = {p.arg for p in func_def_node.args.args}
    missing = [p for p in run_params if p not in forwarded]
    if not missing:
        print("[regen] cli_run_callback: nothing to add")
        return False

    # Build the lines to insert. For each missing param, pick a safe default:
    #   - if cli() already has a same-named param, forward it (``foo=foo``)
    #   - else if run() default is bool ``False``, pass ``False``
    #   - else if run() default is ``None`` or required, pass ``None``
    new_lines: list[str] = []
    for name in missing:
        if name in cli_params:
            new_lines.append(f"        {name}={name},")
            continue
        default = sig.parameters[name].default
        if default is False:
            value_repr = "False"
        elif default is True:
            value_repr = "True"
        elif default == ():
            value_repr = "()"
        else:
            # Empty tuple, None, complex defaults all collapse to None -- this
            # matches the existing pattern (max_cost_usd=None, etc.).
            value_repr = "None"
        new_lines.append(f"        {name}={value_repr},")

    # Insert before the closing ``)`` of the call. The call ends on the line
    # whose unindented end-col equals the call's end_col_offset. Easier: find
    # the slice of source for the call and inject right before the final ``)``.
    if call_node.end_lineno is None or call_node.end_col_offset is None:
        print("[regen] cli_run_callback: AST has no end position", file=sys.stderr)
        return False

    src_lines = source.splitlines(keepends=True)
    end_line_idx = call_node.end_lineno - 1  # 1-indexed -> 0-indexed
    end_line = src_lines[end_line_idx]
    # The end-col-offset points just past the ``)``. Re-find the ``)`` on that
    # line to be robust to trailing whitespace.
    paren_pos = end_line.rfind(")")
    if paren_pos == -1:
        print("[regen] cli_run_callback: could not find closing paren", file=sys.stderr)
        return False

    # Inject the bot-comment + new lines BEFORE the closing-paren line.
    insert_block = "        # Bot-added: drift autofix (regen_contract_drift.py)\n"
    insert_block += "\n".join(new_lines) + "\n"
    src_lines.insert(end_line_idx, insert_block)
    new_source = "".join(src_lines)
    return _write_if_changed(target, source, new_source)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


FIXTURES = {
    "DOCUMENTED_COMMANDS": regen_documented_commands,
    "_INFRASTRUCTURE_PATHS": regen_infrastructure_paths,
    "cli_run_callback": regen_cli_run_callback,
}

# Exit code used when the self-check trips. Anything non-{0,1} is fine; we pick
# 2 to be distinct from "no changes" (1) and "wrote changes" (0).
SELF_CHECK_FAIL_EXIT_CODE = 2


def _run_regen_logic(targets: list[str]) -> bool:
    """Run the requested fixture regen functions and return True iff any wrote."""
    any_changed = False
    for name in targets:
        print(f"[regen] running {name}")
        try:
            changed = FIXTURES[name]()
        except Exception as exc:
            print(f"[regen] {name}: FAILED ({exc.__class__.__name__}: {exc})", file=sys.stderr)
            continue
        any_changed = any_changed or changed
    return any_changed


def _git_diff_is_clean() -> bool:
    """Return True iff the working tree has no unstaged changes.

    Used by the idempotency self-check to detect whether a second regen pass
    introduced new edits on top of an otherwise-clean tree. Falls back to
    ``True`` (clean) when git is unavailable so the self-check still acts as a
    fixture-level guard via the returned ``any_changed`` flag.
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--quiet"],
            capture_output=True,
            cwd=str(REPO_ROOT),
            check=False,
        )
    except (FileNotFoundError, OSError):
        # No git binary; can't compare. Treat as clean and rely on the
        # fixture-level any_changed check below.
        return True
    return result.returncode == 0


def _self_check_idempotent(targets: list[str], initial_clean: bool) -> bool:
    """Re-run regen against just-written tree; return False if non-idempotent.

    A regen step is idempotent when running it against its own output produces
    no further changes. We assert this two ways:

    1. Fixture level: the second pass must report ``any_changed == False``.
    2. Working-tree level: if the tree was clean before the first pass, it must
       remain clean (or at least not gain new diff) after the second pass.

    Returns True when both checks pass, False otherwise.
    """
    print("[regen] self-check: re-running regen to verify idempotency")
    second_any_changed = _run_regen_logic(targets)
    second_clean = _git_diff_is_clean()

    if second_any_changed:
        print(
            "ERROR: regen produced new fixture-level changes on second run (non-idempotent). Aborting.",
            file=sys.stderr,
        )
        return False
    if initial_clean and not second_clean:
        print(
            "ERROR: regen produced new diff on second run (non-idempotent). Aborting.",
            file=sys.stderr,
        )
        return False
    print("[regen] self-check: OK")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture",
        required=True,
        choices=[*FIXTURES.keys(), "all"],
        help="Which contract fixture to regenerate.",
    )
    parser.add_argument(
        "--skip-self-check",
        action="store_true",
        help=(
            "Skip the idempotency self-check that re-runs regen and verifies "
            "no further changes are produced. Useful for debugging."
        ),
    )
    args = parser.parse_args(argv)

    # Make sure we can import bernstein from the repo's src/ layout when
    # invoked from a fresh checkout where the package isn't pip-installed.
    src_dir = REPO_ROOT / "src"
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))

    targets = list(FIXTURES.keys()) if args.fixture == "all" else [args.fixture]

    # Snapshot working-tree cleanliness before the first pass so the self-check
    # can distinguish "we wrote our own diff" from "we keep adding diff on
    # every invocation".
    initial_clean = _git_diff_is_clean()

    any_changed = _run_regen_logic(targets)

    if not args.skip_self_check:
        ok = _self_check_idempotent(targets, initial_clean=initial_clean)
        if not ok:
            return SELF_CHECK_FAIL_EXIT_CODE

    return 0 if any_changed else 1


if __name__ == "__main__":
    raise SystemExit(main())
