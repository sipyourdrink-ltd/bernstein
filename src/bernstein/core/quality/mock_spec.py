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
  give the replacement positionally.

Third-party patch targets (``patch("requests.get")``) and ``patch.object``
are left alone: the first is not code the change can make up, and the second
names its target by object, which this static pass does not resolve.

The check is pure: it reads source text and returns findings. It never
imports or runs the test.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

#: Constructors that build a mock answering to any attribute unless given a spec.
MOCK_CONSTRUCTORS = frozenset({"Mock", "MagicMock", "AsyncMock", "NonCallableMock", "NonCallableMagicMock"})

#: Keywords that bound what a mock constructor answers to.
_CONSTRUCTOR_SPEC_KWARGS = frozenset({"spec", "spec_set"})

#: Keywords that make ``patch`` install something other than a spec-less ``MagicMock``.
_PATCH_SPEC_KWARGS = frozenset({"autospec", "spec", "spec_set", "new", "new_callable"})

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


def find_unspecced_mocks(source: str, path: str, first_party: frozenset[str] = frozenset()) -> list[UnspeccedMock]:
    """Return every mock in ``source`` that is built without a spec.

    Args:
        source: Python source of one test file.
        path: Name to report findings under.
        first_party: Top-level package or module names that belong to the
            project. A ``patch`` target is checked only when its first dotted
            component is one of these.

    Returns:
        Findings in source order. A file that does not parse yields none:
        a syntax error is the ``lint`` and ``tests`` gates' finding, not this one.
    """
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError:
        return []

    findings: list[UnspeccedMock] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _has_star_kwargs(node):
            continue
        callee = _callee_name(node.func)
        if not callee:
            continue
        leaf = callee.rsplit(".", 1)[-1]
        if leaf in MOCK_CONSTRUCTORS:
            if not node.args and not (_keyword_names(node) & _CONSTRUCTOR_SPEC_KWARGS):
                findings.append(UnspeccedMock(path=path, line=node.lineno, call=callee))
        elif leaf == "patch":
            if not node.args or not isinstance(node.args[0], ast.Constant) or not isinstance(node.args[0].value, str):
                continue
            target = node.args[0].value
            # ``patch(target, new)`` gives the replacement positionally.
            if len(node.args) > 1 or _keyword_names(node) & _PATCH_SPEC_KWARGS:
                continue
            if _is_first_party(target, first_party):
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


def scan_test_files(run_dir: Path, test_files: Iterable[str]) -> list[UnspeccedMock]:
    """Scan each existing test file under ``run_dir`` for spec-less mocks.

    Paths that do not exist (a deleted test) or cannot be read are skipped.
    """
    first_party = first_party_names(run_dir)
    findings: list[UnspeccedMock] = []
    for rel in sorted(set(test_files)):
        try:
            source = (run_dir / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        findings.extend(find_unspecced_mocks(source, rel, first_party))
    return findings
