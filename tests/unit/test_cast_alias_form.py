"""Cast targets must be type aliases, not strings that look like types.

`cast("dict[str, Any]", raw)` repeated across a module trips Sonar's
duplicated-string-literal rule (S1192), and the natural way to satisfy that
rule is to hoist the literal into a module constant::

    _CAST_DICT_STR_ANY = "dict[str, Any]"
    ...
    cast(_CAST_DICT_STR_ANY, raw)

That reads as a fix and is not one. `cast()` accepts a string at runtime, so
nothing breaks and no test notices - but the first argument is now a `str`
variable rather than a type, so a type checker cannot resolve it. Every value
cast through such a constant loses its type, and the loss propagates: the
result is treated as `object`, and each attribute access on it becomes its own
error somewhere else in the file. In this repository that single pattern, in
16 files, accounted for 146 of 448 reported errors - a third of the backlog,
almost none of it reported at the line that caused it.

PEP 695 satisfies both tools at once, and this is the form the codebase
already used elsewhere::

    type _CAST_DICT_STR_ANY = dict[str, Any]

There is no duplicated literal for S1192 to flag, and the alias is a real type.

This test exists because the pressure that produced the string form is still
there: the next duplicated-literal sweep will reach for exactly the same
shortcut. It is deliberately narrow - it says nothing about how casts are
named or where aliases live, only that a cast alias must not be a string.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPO_ROOT / "src" / "bernstein"

#: Module-level assignments whose name marks them as a cast target.
_CAST_PREFIX = "_CAST_"


def _string_valued_cast_aliases(tree: ast.Module) -> list[tuple[str, int]]:
    """Return ``(name, lineno)`` for module-level ``_CAST_x = "..."`` bindings.

    Only plain assignment is inspected. An annotated assignment such as
    ``_CAST_X: str = "..."`` is a deliberate string constant and says so.
    """
    found: list[tuple[str, int]] = []
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not isinstance(node.value, ast.Constant) or not isinstance(node.value.value, str):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id.startswith(_CAST_PREFIX):
                found.append((target.id, node.lineno))
    return found


def test_no_cast_alias_is_declared_as_a_string() -> None:
    """A string here type-checks as `str`, so every cast through it is untyped."""
    offenders: list[str] = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - a parse failure is another test's problem
            continue
        offenders += [
            f"{path.relative_to(REPO_ROOT)}:{lineno} {name}" for name, lineno in _string_valued_cast_aliases(tree)
        ]

    assert not offenders, (
        "cast aliases declared as strings:\n  "
        + "\n  ".join(offenders)
        + "\n\nUse a PEP 695 alias instead - `type _CAST_X = dict[str, Any]`. It "
        "carries no duplicated literal for S1192 to flag, and unlike a string "
        "it resolves to a type, so values cast through it keep it."
    )


def test_the_detector_recognises_the_shape_it_is_guarding_against() -> None:
    """Otherwise the assertion above passes by never matching anything."""
    tree = ast.parse('_CAST_DICT_STR_ANY = "dict[str, Any]"\ntype _CAST_OK = dict[str, int]\n_OTHER = "x"\n')
    assert _string_valued_cast_aliases(tree) == [("_CAST_DICT_STR_ANY", 1)]
