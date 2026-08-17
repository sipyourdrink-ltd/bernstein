"""Regenerate contract allow-lists / forwards when CI flags drift.

Three contract tests in ``tests/unit/`` act as drift detectors against the
shape of the public CLI / API / ``cli()`` callback. They fail loud when
something new is added without updating a small allow-list. The fix in each
case is a 1-2 line edit to a data structure -- this script does that edit
automatically so the bot can open a follow-up PR.

Fixtures handled
----------------
* ``DOCUMENTED_COMMANDS``  -- ``tests/unit/test_readme_api_coverage.py``
  (report-only; see ``regen_documented_commands``)
* ``_INFRASTRUCTURE_PATHS`` -- ``tests/unit/test_api_v1_routing.py``
* ``cli_run_callback``     -- forward-arg list in ``src/bernstein/cli/main.py``

Not every fixture is auto-patchable. ``DOCUMENTED_COMMANDS`` guards a
documentation contract, and the only edit that satisfies it without writing
documentation is one no bot should make on a contributor's behalf, so that
fixture reports and stops.

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
import importlib.util
import inspect
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

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

CLI_DOC_GATE_PATH = REPO_ROOT / "tests" / "unit" / "test_readme_api_coverage.py"
CLI_REFERENCE_PATH = REPO_ROOT / "docs" / "reference" / "cli-reference.md"


def _load_cli_doc_gate() -> ModuleType:
    """Import ``tests/unit/test_readme_api_coverage.py`` as a module.

    The gate owns both the exemption map and the rules for reading a command
    out of the CLI reference. Importing it keeps this script from carrying a
    second copy of those rules that can drift away from the one CI enforces.
    """
    spec = importlib.util.spec_from_file_location("_cli_doc_gate_under_regen", CLI_DOC_GATE_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover - import machinery
        raise ImportError(f"cannot load {CLI_DOC_GATE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def regen_documented_commands() -> bool:
    """Report registered CLI commands that are neither documented nor exempt.

    Deliberately report-only: it never writes, and always returns False.

    The gate in ``tests/unit/test_readme_api_coverage.py`` exists so that a
    newly registered command is either documented in
    ``docs/reference/cli-reference.md`` or recorded in
    ``UNDOCUMENTED_EXEMPTIONS`` with a reason someone wrote. Appending a
    bot-authored exemption satisfies the gate without either, and the autofix
    workflow pushes that patch straight onto the source PR's head ref -- so an
    undocumented command would go green with no human in the loop, which is the
    failure mode the gate was rewritten to remove (#3468).

    Leaving the drift unpatched routes it to the workflow's tracking-issue
    path, where a person decides between documenting the command and exempting
    it.
    """
    from bernstein.cli.main import cli

    if not CLI_REFERENCE_PATH.is_file():
        print(
            f"[regen] UNDOCUMENTED_EXEMPTIONS: {CLI_REFERENCE_PATH} is missing -- "
            "refusing to classify commands as undocumented without it.",
            file=sys.stderr,
        )
        return False
    try:
        reference_text = CLI_REFERENCE_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        print(
            f"[regen] UNDOCUMENTED_EXEMPTIONS: cannot read {CLI_REFERENCE_PATH} ({exc}) -- skipping.",
            file=sys.stderr,
        )
        return False

    gate = _load_cli_doc_gate()
    documented = gate._parse_documented_commands(reference_text)
    exemptions: dict[str, str] = gate.UNDOCUMENTED_EXEMPTIONS
    registered = set(cli.commands)
    reference = CLI_REFERENCE_PATH.name

    # Every invariant the gate enforces, not just the missing-command one:
    # reporting "nothing to add" while CI is red on a stale or malformed
    # exemption would send the reader looking in the wrong place.
    problems: list[str] = []
    if missing := sorted(registered - (documented | set(exemptions))):
        problems.append(f"registered, but neither documented in {reference} nor exempt: {', '.join(missing)}")
    if redundant := sorted(set(exemptions) & documented):
        problems.append(f"exempt, but now documented in {reference} -- drop the exemption: {', '.join(redundant)}")
    if phantoms := sorted(set(exemptions) - registered):
        problems.append(f"exempt, but not a registered command: {', '.join(phantoms)}")
    if blank := sorted(name for name, reason in exemptions.items() if not reason or not reason.strip()):
        problems.append(f"exempt without a reason: {', '.join(blank)}")

    if not problems:
        print("[regen] UNDOCUMENTED_EXEMPTIONS: nothing to add")
        return False

    for problem in problems:
        print(f"[regen] UNDOCUMENTED_EXEMPTIONS: {problem}", file=sys.stderr)
    print(
        "[regen] This fixture does not patch itself: an exemption records a decision, and a "
        "bot-written one records none. Resolve the above in "
        f"{CLI_DOC_GATE_PATH.relative_to(REPO_ROOT)} or {CLI_REFERENCE_PATH.relative_to(REPO_ROOT)}.",
        file=sys.stderr,
    )
    return False


# ---------------------------------------------------------------------------
# Fixture 2: _INFRASTRUCTURE_PATHS
# ---------------------------------------------------------------------------


def regen_infrastructure_paths() -> bool:
    """Add new root-only FastAPI routes to the parity allow-list."""
    from fastapi.routing import APIRoute, APIWebSocketRoute
    from starlette.routing import WebSocketRoute

    from bernstein.core.routes.route_table import iter_route_paths
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
        for path, route in iter_route_paths(app):
            if not isinstance(route, APIRoute | APIWebSocketRoute | WebSocketRoute):
                continue
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
