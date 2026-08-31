"""No module in ``core/orchestration/`` may carry an unreachable function.

ORCH-009 extracted the run loop into ``orchestrator_run.py`` and never switched
``Orchestrator.run()`` over to it. Nothing called the extracted copy, so 209 of
that module's 306 lines sat unreachable for long enough to drift: it hardcoded
the failure ceiling the live loop reads from config, never recorded
``RunClosureOutcome.FAILED``, and kept a bare ``time.sleep`` after #4872 gave the
live loop its ``_pace`` seam. The repository held both a bug and its fix, with
only one of them reachable (#4882).

Deleting that copy does not stop the next half-finished extraction leaving
another. Nothing fails when code is merely unreachable - that is the whole
problem - so the guard has to be a test that goes looking.

**Reachability, not reference count.** A private helper called only inside its
own module is perfectly alive; what killed ``orchestrator_run`` was a PUBLIC
entry point with no callers, dragging a private subtree down with it. So roots
are the symbols something outside the module names (plus module-level and class
body code), and everything reachable from a root is live. Counting references
would flag ~264 healthy private helpers and be deleted within a week.

The allowlist is the 18 functions already unreachable when this landed. It may
only ever SHRINK: ``test_allowlist_has_no_stale_entries`` fails if an entry
becomes reachable, so fixing one forces its removal rather than letting the list
rot into a permanent exemption.
"""

from __future__ import annotations

import ast
import re
from collections import defaultdict
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = REPO_ROOT / "src" / "bernstein" / "core" / "orchestration"
SEARCHED = ("src", "tests", "scripts")

#: The 35 already unreachable when this guard landed. Pre-existing debt, deliberately NOT
#: fixed here: this change removes one dead run loop, and deleting three more modules in
#: the same breath would put unrelated judgement calls behind one review.
#:
#: Three of these are the ORCH-009 shape again, which is the argument for the guard:
#: `orchestrator_backlog.ingest_backlog` is dead while `Orchestrator.ingest_backlog`
#: (orchestrator.py:5276) runs, and `engagement_projection.canonical_graph_digest` is dead
#: while `schedule_projection.canonical_graph_digest` runs. Same extraction, same abandoned
#: delegation, same silent divergence waiting to happen.
#:
#: SHRINK ONLY - `test_allowlist_has_no_stale_entries` fails if an entry becomes reachable.
KNOWN_UNREACHABLE: frozenset[str] = frozenset(
    {
        "engagement_projection.py:_build_mandate",
        "engagement_projection.py:_canonical_nodes",
        "engagement_projection.py:_node_to_dict",
        "engagement_projection.py:canonical_graph_bytes",
        "engagement_projection.py:canonical_graph_digest",
        "engagement_projection.py:project",
        "holds.py:has_active_holds",
        "orchestrator_backlog.py:_claim_backlog_file",
        "orchestrator_backlog.py:_collect_backlog_files",
        "orchestrator_backlog.py:_ensure_ingested_titles",
        "orchestrator_backlog.py:_ingest_backlog_one_by_one",
        "orchestrator_backlog.py:_parse_candidates",
        "orchestrator_backlog.py:ingest_backlog",
        "phase_gates.py:_extract_edges",
        "phase_gates.py:_has_cycle",
        "phase_gates.py:_r001",
        "phase_gates.py:_r002",
        "phase_gates.py:_r003",
        "phase_gates.py:_r004",
        "phase_gates.py:_r005",
        "phase_gates.py:register_rule",
        "run_actor_registry.py:register",
        "run_actor_registry.py:unregister",
        "tick_pipeline.py:check_nudges_during_tick",
        "tracker_pipeline.py:build_pipeline_from_yaml",
        "tracker_pipeline.py:default_ledger_path",
        "tracker_pipeline.py:stage_attempt_for",
        "worker.py:check_token_escalation",
        "worker.py:register_permission_hook",
        "workload_prediction.py:_analyze_backlog",
        "workload_prediction.py:_breakdown_by_role",
        "workload_prediction.py:_calculate_recommended_agents",
        "workload_prediction.py:_get_historical_metrics",
        "workload_prediction.py:format_workload_report",
        "workload_prediction.py:predict_workload",
    }
)

pytestmark = pytest.mark.skipif(
    not PACKAGE.is_dir(),
    reason="reachability guard only runs inside a bernstein source checkout",
)

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


#: This file names every allowlisted symbol, so counting itself would make each one look
#: referenced from another module - and the allowlist would silently root everything it
#: was meant to record. Excluded rather than worked around: a guard that reads its own
#: text is measuring itself.
SELF = Path(__file__).resolve()

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _reference_index() -> dict[str, set[str]]:
    """``{module_stem: {names other files reach through it}}``.

    Read off the AST, not the text. Two earlier shapes of this were wrong in opposite
    directions, and both are worth naming because either would have shipped a guard that
    lies:

    A **bare-name** search roots a dead function whenever any file defines a method with
    the same name - and that is this codebase's normal state, not a corner case:
    ``orchestrator_run._run_loop`` shares its name with methods on ``run_actor`` and
    ``mcp_health_monitor``, ``_has_active_agents`` with one on ``Orchestrator``. It reports
    the exact defect it was built to find as reachable.

    A **text** search over stripped source has the mirror flaw: prose is not a call, but
    stripping comments and docstrings to fix that also breaks up ``mod.attr`` tokens, and
    the live dependency-scan shims here then look dead. A guard that says live code is
    unreachable is the more dangerous of the two - someone deletes it.

    The AST has neither problem. ``mod.attr`` is an Attribute on a Name, prose is not a
    node at all, and ``from x.mod import a`` names ``a`` exactly.
    """
    refs: dict[str, set[str]] = defaultdict(set)
    for top in SEARCHED:
        for path in (REPO_ROOT / top).rglob("*.py"):
            if path == SELF:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (OSError, SyntaxError):
                continue
            own = path.stem if path.parent == PACKAGE else None
            for node in ast.walk(tree):
                # `mod.name` - an attribute read off a module-named binding.
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                    if node.value.id != own:
                        refs[node.value.id].add(node.attr)
                # `from pkg.mod import a, b`
                elif isinstance(node, ast.ImportFrom) and node.module is not None:
                    stem = node.module.rsplit(".", 1)[-1]
                    if stem != own:
                        for alias in node.names:
                            refs[stem].add(alias.name)
    return refs


def _unreachable_in(path: Path, source: str, refs: dict[str, set[str]]) -> list[str]:
    """Module-level functions not reachable from anything that roots them."""
    tree = ast.parse(source)
    funcs = {node.name: node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    if not funcs:
        return []

    def names_in(node: ast.AST) -> set[str]:
        """Identifiers actually USED in `node`, never words inside its strings.

        This read `_IDENT.findall(ast.unparse(node))`, which sweeps up string contents -
        and a module docstring is a string. `orchestrator_run`'s own opening line, "Orchestrator
        run loop: startup, main loop, and shutdown coordination", contains the word `run`, so
        the dead `run()` rooted itself from its own docstring and carried its whole private
        subtree with it. Eleven of the twelve dead functions read as reachable.
        """
        used: set[str] = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name):
                used.add(sub.id)
            elif isinstance(sub, ast.Attribute):
                used.add(sub.attr)
        return used & funcs.keys()

    edges = {name: names_in(node) - {name} for name, node in funcs.items()}

    roots: set[str] = set()
    for node in tree.body:
        # Module-level statements and class bodies can both call these, and neither is a
        # function, so neither shows up in the call graph above.
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            roots |= names_in(node)
    roots |= refs.get(path.stem, set()) & funcs.keys()

    seen: set[str] = set()
    stack = list(roots)
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(edges.get(current, ()))
    return sorted(set(funcs) - seen)


def _scan() -> dict[str, list[str]]:
    refs = _reference_index()
    found: dict[str, list[str]] = {}
    for path in sorted(PACKAGE.glob("*.py")):
        if path.name == "__init__.py":
            continue
        dead = _unreachable_in(path, path.read_text(encoding="utf-8"), refs)
        if dead:
            found[path.name] = dead
    return found


def test_no_new_unreachable_functions_in_orchestration() -> None:
    """A half-finished extraction must fail here rather than sit for a release."""
    found = {f"{module}:{name}" for module, names in _scan().items() for name in names}
    new = sorted(found - KNOWN_UNREACHABLE)
    assert not new, (
        "unreachable function(s) in core/orchestration/ that nothing can call: "
        f"{new}. Either wire them up or delete them; a copy nothing calls drifts from the "
        "one that runs, which is how ORCH-009 left two run loops (#4882)."
    )


def test_allowlist_has_no_stale_entries() -> None:
    """The allowlist may only shrink, so a fix is forced to remove its entry."""
    found = {f"{module}:{name}" for module, names in _scan().items() for name in names}
    stale = sorted(KNOWN_UNREACHABLE - found)
    assert not stale, (
        f"these are no longer unreachable: {stale}. Remove them from KNOWN_UNREACHABLE - "
        "an exemption that outlives its reason is how the list stops meaning anything."
    )


def test_the_deleted_run_loop_stays_deleted() -> None:
    """The specific regression: orchestrator_run must hold no run loop again."""
    found = _scan().get("orchestrator_run.py", [])
    assert not found, f"orchestrator_run.py has unreachable functions again: {found}"
