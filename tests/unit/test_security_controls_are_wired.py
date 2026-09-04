"""A security control with no caller is not a control (#4992).

`core/security/` holds modules that look installed: complete implementations,
result types, `should_block` flags, audit-record writers. What several of them
do not have is a call site, and nothing about that is red.

`post_tool_enforcement.run_post_tool_enforcement` inspects tool output for
secrets and PII, redacts before persistence, writes an audit record and can
block. Its docstring says it mirrors the pre-tool `check_secrets` flow. The
pre-tool half runs; the mirror has never been called.

**Two lists, not one.** A first version of this guard reported a single list and
was wrong in the dangerous direction: its reference index was keyed on the
binding name, so `import capability_tokens as _cap` hid every `_cap.mint_root(...)`
call and forty live functions were reported dead. On security controls the
natural response to that report is to wire or delete the subject.

Resolving aliases makes the index bigger, not correct: the next miss is a call
reached through `getattr`, a registry dict, or a plugin entry point, and no
import-graph walk sees any of those. So the guard distinguishes what it PROVED
from what it merely could not see:

- **proved uncalled** - the name appears nowhere outside its own module, in any
  file, in any form: not as code, not in a string, not in a comment. Nothing
  dynamic can dispatch to a name that is written down nowhere. This fails.
- **unproven** - no static reference, but the name IS written somewhere. A
  registry key, a doc, a config value. It may well be dead; this guard cannot
  say so, and it prints rather than failing.

The second list is where the value is. Without it the first `getattr` dispatch
in this package turns the guard into a deletion machine.
"""

from __future__ import annotations

import ast
import re
from collections import defaultdict
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = REPO_ROOT / "src" / "bernstein" / "core" / "security"
SEARCHED = ("src", "tests", "scripts")
SELF = Path(__file__).resolve()

#: The 20 still PROVED uncalled: each name appears nowhere outside its own module, in
#: any file, in any form. Pre-existing debt, deliberately not fixed here - each needs
#: its own judgement about wiring versus deleting. The two `post_tool_enforcement`
#: entries the list started with are gone: #4992 wired them into the hook receiver's
#: `PostToolUse` branch, which is what `test_no_stale_exemptions` is for.
#:
#: Every entry was sampled against an independent bare-name grep before being written
#: down. The first version of this guard was not, and reported forty live functions dead.
#:
#: SHRINK ONLY - `test_no_stale_exemptions` fails once one gains a reference.
KNOWN_UNCALLED: frozenset[str] = frozenset(
    {
        "audit_chain.py:record_expectation_expired",
        "audit_chain.py:record_pool_claim_receipt",
        "audit_chain.py:record_pool_retired",
        "audit_chain.py:record_schedule_collision",
        "auth_middleware.py:check_agent_task_scope_ids",
        "capability_tokens.py:path_covered_by",
        "deployment_profile.py:installed_sovereign_public_key",
        "eu_ai_act.py:read_assessment_records",
        "guardrails.py:check_critical_file_modifications",
        "guardrails.py:check_review_checklist",
        "intent_capsule.py:iter_module_import_names",
        "intent_capsule.py:normalise_tool_name",
        "permission_matrix.py:log_resolution",
        "permission_policy.py:load_permissions_config",
        "promptware_ingest.py:get_default_detector",
        "rbac.py:require_permission",
        "rbac.py:require_role",
        "secrets_broker.py:unregister_secret_for_redaction",
        "socket_guard.py:collect_unmonitored_destinations",
        "tenanting.py:build_tenant_registry",
    }
)

pytestmark = pytest.mark.skipif(
    not PACKAGE.is_dir(),
    reason="security wiring guard only runs inside a bernstein source checkout",
)

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _module_aliases(tree: ast.AST) -> dict[str, str]:
    """``{binding: module_stem}`` for every import in this file.

    The half the first version of this guard was missing. ``import a.b.c as x``
    and ``from a import b as x`` both bind a name that is not the module's, and an
    index keyed on the binding reports every call through it as a call to nothing.
    """
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                stem = a.name.rsplit(".", 1)[-1]
                aliases[a.asname or stem] = stem
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            parent = node.module.rsplit(".", 1)[-1]
            for a in node.names:
                # `from pkg import mod as x` binds a MODULE; `from pkg.mod import fn`
                # binds a function. Both are recorded: the first maps x -> mod, and the
                # second is picked up as a direct reference below.
                aliases[a.asname or a.name] = a.name
                aliases.setdefault(parent, parent)
    return aliases


def _static_references() -> dict[str, set[str]]:
    """``{module_stem: {names reached through it statically}}``, alias-aware."""
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
            aliases = _module_aliases(tree)
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                    stem = aliases.get(node.value.id, node.value.id)
                    if stem != own:
                        refs[stem].add(node.attr)
                elif isinstance(node, ast.ImportFrom) and node.module is not None:
                    stem = node.module.rsplit(".", 1)[-1]
                    if stem != own:
                        for a in node.names:
                            refs[stem].add(a.name)
    return refs


def _written_anywhere() -> set[str]:
    """Every identifier-shaped token appearing outside `core/security/`.

    Deliberately the crudest possible search, over code AND prose AND strings.
    A name written nowhere cannot be reached by any dispatch, static or dynamic;
    that is the only claim this guard is willing to fail CI on.
    """
    seen: set[str] = set()
    for top in SEARCHED:
        for path in (REPO_ROOT / top).rglob("*.py"):
            if path == SELF or path.parent == PACKAGE:
                continue
            try:
                seen.update(_IDENT.findall(path.read_text(encoding="utf-8")))
            except OSError:
                continue
    return seen


def _classify() -> tuple[set[str], set[str]]:
    """``(proved_uncalled, unproven)`` over public functions in `core/security/`."""
    refs = _static_references()
    written = _written_anywhere()
    proved: set[str] = set()
    unproven: set[str] = set()
    for path in sorted(PACKAGE.glob("*.py")):
        if path.name == "__init__.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        reached = refs.get(path.stem, set())
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name.startswith("_") or node.decorator_list:
                # Private helpers are reached through their module's own surface, and a
                # decorated function is reachable by construction - the decorator runs at
                # import and can register it anywhere.
                continue
            if node.name in reached:
                continue
            entry = f"{path.name}:{node.name}"
            (proved if node.name not in written else unproven).add(entry)
    return proved, unproven


def test_no_security_control_is_proved_uncalled() -> None:
    """Fails only on what the guard can prove: a name written nowhere else at all."""
    proved, _unproven = _classify()
    new = sorted(proved - KNOWN_UNCALLED)
    assert not new, (
        "public entry point(s) in core/security/ that nothing anywhere references, in code "
        f"or in prose: {new}. A security control with no call site is not a control - wire "
        "it, or delete it, or add it to KNOWN_UNCALLED with the reason."
    )


def test_no_stale_exemptions() -> None:
    """The exemption list may only shrink."""
    proved, _unproven = _classify()
    stale = sorted(KNOWN_UNCALLED - proved)
    assert not stale, (
        f"these are no longer proved-uncalled: {stale}. Remove them from KNOWN_UNCALLED - "
        "an exemption that outlives its reason is how the list stops meaning anything."
    )


def test_the_unproven_list_is_reported_and_never_fails() -> None:
    """Named, not enforced.

    A control reachable only through `getattr`, a registry dict or a plugin entry point is
    invisible to any import-graph walk. Failing on it would make the first dynamic dispatch
    in this package turn the guard into a deletion machine - so this list is printed and
    the build carries on.
    """
    _proved, unproven = _classify()
    if unproven:
        print(
            "\nunproven (no static reference, but the name is written somewhere - "
            "may be reached dynamically):\n  " + "\n  ".join(sorted(unproven))
        )


def test_the_guard_can_see_a_caller_it_should() -> None:
    """The guard must not pass by finding nothing to check.

    A broken index reports zero and looks green. `check_secrets` - the pre-tool half of
    the flow #4992 is about - is called from `guardrails.py`, so it must resolve.
    """
    assert "check_secrets" in _static_references().get("guardrails", set())


def test_alias_resolution_actually_resolves() -> None:
    """The specific bug that killed the first version of this guard.

    `permission_delegation.py` calls `_cap.mint_root(...)` through
    `import ... capability_tokens as _cap`. Keyed on the binding name, that call is
    invisible and forty live functions were reported dead.
    """
    assert "mint_root" in _static_references().get("capability_tokens", set())
