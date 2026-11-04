"""Find mocks in agent-written tests that do not name what they stand for (#6150).

A bare ``MagicMock()`` answers to any attribute name, so a test can configure
``mock.non_existent_method_xyz.return_value = 100``, assert it back, and pass
without touching the code it claims to test. The ``tests`` gate cannot see
this: a test ran and passed, which is all it measures.

Detecting the bad assertion after the fact would catch some shapes and miss
others. Requiring a spec makes the failure impossible at run time instead:
``create_autospec(PaymentService)``, ``MagicMock(spec=PaymentService)`` or
``patch("pkg.mod.Name", autospec=True)`` raise ``AttributeError`` on an
attribute the real object does not have. So the rule this module checks is
narrow and mechanical:

* a ``Mock`` / ``MagicMock`` / ``AsyncMock`` / ``NonCallableMock`` /
  ``NonCallableMagicMock`` call must pass a spec (the first positional
  argument, ``spec=`` or ``spec_set=``);
* a ``patch(...)`` whose target string names a first-party module must pass
  ``autospec=``, ``spec=``, ``spec_set=``, ``new=`` or ``new_callable=``, or
  give the replacement as its second positional argument.

A spec has to bind something: ``spec=None``, ``autospec=False`` and
``new=DEFAULT`` are what the call means with the keyword left out, so they do
not count. Names imported from ``unittest.mock`` or ``mock`` are resolved first,
so ``from unittest.mock import MagicMock as M`` is still ``MagicMock``.

Not checked, on purpose: third-party patch targets (``patch("requests.get")``)
are not code the change can make up; ``patch.object`` and ``patch.multiple``
name their target by object, and a non-literal target (``patch(f"{MOD}.f")``)
has no text to read, so this static pass cannot resolve them; ``conftest.py`` is
not a test module by :func:`is_test_file`.

Only what the change ADDED is judged. Adding a spec to an old mock is not a
neutral edit (a mock configured off-spec starts to raise), so a finding on an
untouched line would push an agent into an unsafe drive-by change. The caller
passes the added lines per file; a call is in scope when any line of it was
added. With no history to read them from, the whole file is scanned: the gate
then has no way to tell old from new, and a miss is the worse failure.

The check is pure: it reads source text and returns findings. It never
imports or runs the test.
"""

from __future__ import annotations

import ast
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

logger = logging.getLogger(__name__)

#: Constructors that build a mock answering to any attribute unless given a spec.
MOCK_CONSTRUCTORS = frozenset({"Mock", "MagicMock", "AsyncMock", "NonCallableMock", "NonCallableMagicMock"})

#: Keywords that bound what a mock constructor answers to.
_CONSTRUCTOR_SPEC_KWARGS = frozenset({"spec", "spec_set"})

#: Keywords that make ``patch`` install something other than a spec-less ``MagicMock``.
_PATCH_SPEC_KWARGS = frozenset({"autospec", "spec", "spec_set", "new", "new_callable"})

#: The modules a mock can be imported from.
_MOCK_MODULES = frozenset({"unittest.mock", "mock"})

#: Finding line prefix. The gate-evasion suite matches on it, so it is part of
#: the gate's output contract.
FINDING_TEXT = "mock without a spec"


@dataclass(frozen=True)
class UnspeccedMock:
    """One mock that answers to any attribute name.

    Attributes:
        path: Test file path as the caller named it.
        line: 1-based line of the call.
        call: The callee as written, e.g. ``MagicMock`` or ``mock.patch``.
        target: The ``patch`` target string, or ``""`` for a constructor.
    """

    path: str
    line: int
    call: str
    target: str = ""

    def render(self) -> str:
        """Return the finding as one ``path:line: ...`` line."""
        what = f'{self.call}("{self.target}")' if self.target else f"{self.call}()"
        remedy = "pass autospec=True" if self.target else "pass spec= or spec_set=, or build it with create_autospec()"
        return f"{self.path}:{self.line}: {FINDING_TEXT}: {what}; {remedy}"


def _callee_name(func: ast.expr) -> str:
    """Return ``func`` as dotted text (``MagicMock``, ``mock.patch``), or ``""``."""
    parts: list[str] = []
    node = func
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return ""
    parts.append(node.id)
    return ".".join(reversed(parts))


def _keyword_names(call: ast.Call) -> set[str]:
    return {kw.arg for kw in call.keywords if kw.arg is not None}


def _has_star_kwargs(call: ast.Call) -> bool:
    # ``MagicMock(**opts)`` may carry a spec this pass cannot see.
    return any(kw.arg is None for kw in call.keywords) or any(isinstance(a, ast.Starred) for a in call.args)


def _is_first_party(target: str, first_party: frozenset[str]) -> bool:
    return target.split(".", 1)[0] in first_party


def _binds_nothing(value: ast.expr) -> bool:
    """Whether a spec argument leaves the mock as open as omitting it would."""
    if isinstance(value, ast.Constant):
        return value.value is None or value.value is False
    return _callee_name(value).rsplit(".", 1)[-1] == "DEFAULT"


def _bound_keywords(call: ast.Call, names: frozenset[str]) -> bool:
    return any(kw.arg in names and not _binds_nothing(kw.value) for kw in call.keywords)


def _mock_aliases(tree: ast.AST) -> dict[str, str]:
    """Local names bound to something from a mock module, mapped to its real leaf name."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in _MOCK_MODULES:
            aliases.update({alias.asname: alias.name for alias in node.names if alias.asname})
    return aliases


def _constructor_is_unspecced(call: ast.Call) -> bool:
    if call.args and not _binds_nothing(call.args[0]):
        return False
    return not _bound_keywords(call, _CONSTRUCTOR_SPEC_KWARGS)


def _patch_target(call: ast.Call, first_party: frozenset[str]) -> str | None:
    """The first-party target of a ``patch`` that installs a spec-less mock, else None."""
    if not call.args or not isinstance(call.args[0], ast.Constant) or not isinstance(call.args[0].value, str):
        return None
    target = call.args[0].value
    replaced = len(call.args) > 1 and not _binds_nothing(call.args[1])
    if replaced or _bound_keywords(call, _PATCH_SPEC_KWARGS) or not _is_first_party(target, first_party):
        return None
    return target


def _in_scope(node: ast.Call, added: frozenset[int] | None) -> bool:
    """Whether any line of the call was added. ``None`` means no history: every line counts."""
    if added is None:
        return True
    return any(line in added for line in range(node.lineno, (node.end_lineno or node.lineno) + 1))


def find_unspecced_mocks(
    source: str,
    path: str,
    first_party: frozenset[str] = frozenset(),
    added: frozenset[int] | None = None,
) -> list[UnspeccedMock]:
    """Return every mock in ``source`` that is built without a spec.

    Args:
        source: Python source of one test file.
        path: Name to report findings under.
        first_party: Top-level package or module names that belong to the
            project. A ``patch`` target is checked only when its first dotted
            component is one of these.
        added: The 1-based lines the change added, or ``None`` to judge every
            line. A call spanning several lines is in scope if any was added.

    Returns:
        Findings in source order. A file that does not parse yields none:
        a syntax error is the ``lint`` and ``tests`` gates' finding, not this one.
    """
    try:
        tree = ast.parse(source, filename=path)
    except (SyntaxError, ValueError, RecursionError):
        return []

    aliases = _mock_aliases(tree)
    findings: list[UnspeccedMock] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _has_star_kwargs(node) or not _in_scope(node, added):
            continue
        callee = _callee_name(node.func)
        if not callee:
            continue
        written = callee.rsplit(".", 1)[-1]
        leaf = aliases.get(written, written)
        if leaf in MOCK_CONSTRUCTORS:
            if _constructor_is_unspecced(node):
                findings.append(UnspeccedMock(path=path, line=node.lineno, call=callee))
        elif leaf == "patch":
            target = _patch_target(node, first_party)
            if target is not None:
                findings.append(UnspeccedMock(path=path, line=node.lineno, call=callee, target=target))
    findings.sort(key=lambda f: (f.line, f.call))
    return findings


def first_party_names(run_dir: Path) -> frozenset[str]:
    """Return the top-level importable names the project at ``run_dir`` owns.

    Packages and modules directly under ``src/`` (the src layout) and at the
    repository root count. ``tests`` and private names do not.
    """
    names: set[str] = set()
    for base in (run_dir / "src", run_dir):
        if not base.is_dir():
            continue
        for entry in base.iterdir():
            stem = entry.stem if entry.suffix == ".py" else entry.name
            if stem.startswith(("_", ".")) or stem in {"tests", "test", "src"}:
                continue
            if (entry.is_dir() and (entry / "__init__.py").is_file()) or (entry.is_file() and entry.suffix == ".py"):
                names.add(stem)
    return frozenset(names)


def is_test_file(path: str) -> bool:
    """Return whether ``path`` names a pytest test module."""
    name = Path(path).name
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def scan_test_files(
    run_dir: Path,
    test_files: Iterable[str],
    added_lines: Mapping[str, frozenset[int]] | None = None,
) -> list[UnspeccedMock]:
    """Scan each test file under ``run_dir`` for spec-less mocks.

    Args:
        run_dir: The worktree the paths are relative to.
        test_files: Paths to scan.
        added_lines: The lines the change added, per path. ``None`` means there
            is no history and every file is scanned whole. With a mapping, a
            path it does not name has nothing added (a pure rename) and is
            skipped. A path that resolves outside ``run_dir``, or that is
            deleted or unreadable, is skipped and logged at debug level.
    """
    first_party = first_party_names(run_dir)
    root = run_dir.resolve()
    findings: list[UnspeccedMock] = []
    for rel in sorted(set(test_files)):
        added = None if added_lines is None else added_lines.get(rel)
        if added_lines is not None and added is None:
            logger.debug("mock spec check: %s has no added lines", rel)
            continue
        path = (run_dir / rel).resolve()
        if not path.is_relative_to(root):
            logger.debug("mock spec check: skipping %s, it resolves outside the worktree", rel)
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            logger.debug("mock spec check: skipping %s: %s", rel, exc)
            continue
        findings.extend(find_unspecced_mocks(source, rel, first_party, added))
    return findings
