"""Guard: a public security/identity symbol must be reached from production.

``scripts/check_unreachable_controls.py`` fails when a public symbol under
``src/bernstein/core/security/`` or ``src/bernstein/core/identity/`` has no
caller outside tests, ``__init__`` re-exports, and the lazy module map in
``src/bernstein/core/__init__.py`` (issue #5053).

Every test here builds a miniature package tree on disk and runs the real
``main()`` over it, so the failure path executes rather than being asserted
about. The last test runs the gate over this repository, which is what binds
the committed allowlist to the tree.
"""

from __future__ import annotations

import importlib.util
import sys
import textwrap
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_unreachable_controls.py"


@pytest.fixture(scope="module")
def check_module() -> ModuleType:
    """Load ``scripts/check_unreachable_controls.py`` as an importable module."""
    spec = importlib.util.spec_from_file_location("check_unreachable_controls_under_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    return path


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A minimal ``src/bernstein`` tree with the two scanned packages present."""
    for package in ("core/security", "core/identity"):
        _write(tmp_path / "src/bernstein" / package / "__init__.py", '"""package."""\n')
    _write(tmp_path / "src/bernstein/core/__init__.py", '"""core."""\n')
    return tmp_path


def _run(check_module: ModuleType, tree: Path, *extra: str) -> int:
    return check_module.main(["--repo-root", str(tree), *extra])


def test_uncalled_public_function_fails_the_gate(
    check_module: ModuleType, tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A throwaway uncalled public control fails; deleting it makes the gate pass.

    This is the acceptance test named in issue #5053: the gate has to change
    verdict on the presence of one uncalled function and nothing else.
    """
    module = tree / "src/bernstein/core/security/throwaway_control.py"
    _write(
        module,
        '''
        """Throwaway."""


        def enforce_throwaway_control() -> bool:
            return True
        ''',
    )

    assert _run(check_module, tree) == 1
    assert "enforce_throwaway_control" in capsys.readouterr().err

    module.unlink()
    assert _run(check_module, tree) == 0


def test_symbol_called_from_a_production_module_passes(check_module: ModuleType, tree: Path) -> None:
    """A control a shipped module actually calls is not a finding."""
    _write(
        tree / "src/bernstein/core/security/live_control.py",
        '''
        """Live."""


        def enforce_live_control() -> bool:
            return True
        ''',
    )
    _write(
        tree / "src/bernstein/core/orchestration/runner.py",
        '''
        """Runner."""

        from bernstein.core.security.live_control import enforce_live_control


        def run() -> bool:
            return enforce_live_control()
        ''',
    )

    assert _run(check_module, tree) == 0


def test_symbol_called_only_from_tests_is_reported(
    check_module: ModuleType, tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A passing unit test is not a production caller."""
    _write(
        tree / "src/bernstein/core/security/tested_control.py",
        '''
        """Tested."""


        def enforce_tested_control() -> bool:
            return True
        ''',
    )
    _write(
        tree / "tests/unit/test_tested_control.py",
        '''
        """Test."""

        from bernstein.core.security.tested_control import enforce_tested_control


        def test_it() -> None:
            assert enforce_tested_control()
        ''',
    )

    assert _run(check_module, tree) == 1
    assert "enforce_tested_control" in capsys.readouterr().err


def test_symbol_reexported_by_init_only_is_reported(
    check_module: ModuleType, tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An ``__init__`` re-export is an import and an ``__all__`` string, not a call."""
    _write(
        tree / "src/bernstein/core/identity/reexported_control.py",
        '''
        """Re-exported."""

        __all__ = ["verify_reexported_control"]


        def verify_reexported_control() -> bool:
            return True
        ''',
    )
    _write(
        tree / "src/bernstein/core/identity/__init__.py",
        '''
        """Package."""

        from bernstein.core.identity.reexported_control import verify_reexported_control

        __all__ = ["verify_reexported_control"]
        ''',
    )

    assert _run(check_module, tree) == 1
    assert "verify_reexported_control" in capsys.readouterr().err


def test_symbol_named_only_in_the_lazy_module_map_is_reported(
    check_module: ModuleType, tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The lazy ``core/__init__.py`` map holds strings, so it reaches nothing."""
    _write(
        tree / "src/bernstein/core/security/mapped_control.py",
        '''
        """Mapped."""


        def enforce_mapped_control() -> bool:
            return True
        ''',
    )
    _write(
        tree / "src/bernstein/core/__init__.py",
        '''
        """Core."""

        _REDIRECT_MAP = {
            "mapped_control": "bernstein.core.security.mapped_control",
            "enforce_mapped_control": "bernstein.core.security.mapped_control",
        }
        ''',
    )

    assert _run(check_module, tree) == 1
    assert "enforce_mapped_control" in capsys.readouterr().err


def test_symbol_called_only_from_an_unreachable_symbol_is_reported(
    check_module: ModuleType, tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Reachability is a fixpoint: a caller that is itself dead reaches nothing."""
    _write(
        tree / "src/bernstein/core/security/chained_control.py",
        '''
        """Chained."""


        def enforce_inner_control() -> bool:
            return True


        def enforce_outer_control() -> bool:
            return enforce_inner_control()
        ''',
    )

    assert _run(check_module, tree) == 1
    reported = capsys.readouterr().err
    assert "enforce_outer_control" in reported
    assert "enforce_inner_control" in reported


def test_allowlisted_symbol_does_not_make_its_callee_reachable(
    check_module: ModuleType, tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Allowlisting silences one symbol; it does not revive what that symbol calls."""
    _write(
        tree / "src/bernstein/core/security/chained_control.py",
        '''
        """Chained."""


        def enforce_inner_control() -> bool:
            return True


        def enforce_outer_control() -> bool:
            return enforce_inner_control()
        ''',
    )
    _write(
        tree / "unreachable_controls_allowlist.txt",
        """
        src/bernstein/core/security/chained_control.py::enforce_outer_control  # planned wiring
        """,
    )

    assert _run(check_module, tree) == 1
    reported = capsys.readouterr().err
    assert "enforce_inner_control" in reported
    assert "enforce_outer_control" not in reported


def test_uncalled_method_of_a_reachable_class_is_reported(
    check_module: ModuleType, tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A live class can still carry a control method nothing ever calls."""
    _write(
        tree / "src/bernstein/core/security/graph_control.py",
        '''
        """Graph."""


        class DecisionGraphStub:
            def evaluate_decision_graph(self) -> bool:
                return True
        ''',
    )
    _write(
        tree / "src/bernstein/core/orchestration/runner.py",
        '''
        """Runner."""

        from bernstein.core.security.graph_control import DecisionGraphStub


        def run() -> DecisionGraphStub:
            return DecisionGraphStub()
        ''',
    )

    assert _run(check_module, tree) == 1
    reported = capsys.readouterr().err
    assert "DecisionGraphStub.evaluate_decision_graph" in reported


def test_allowlisted_symbol_passes_the_gate(check_module: ModuleType, tree: Path) -> None:
    """An entry with a written reason silences its finding."""
    _write(
        tree / "src/bernstein/core/security/listed_control.py",
        '''
        """Listed."""


        def enforce_listed_control() -> bool:
            return True
        ''',
    )
    _write(
        tree / "unreachable_controls_allowlist.txt",
        """
        # header comment
        src/bernstein/core/security/listed_control.py::enforce_listed_control  # offline verifier, no runtime path
        """,
    )

    assert _run(check_module, tree) == 0


@pytest.mark.parametrize(
    "entry",
    [
        "src/bernstein/core/security/listed_control.py::enforce_listed_control",
        "src/bernstein/core/security/listed_control.py::enforce_listed_control  #",
        "src/bernstein/core/security/listed_control.py::enforce_listed_control  # REASON REQUIRED",
    ],
    ids=["no-comment", "empty-comment", "update-marker"],
)
def test_allowlist_entry_without_a_reason_fails(
    check_module: ModuleType, tree: Path, capsys: pytest.CaptureFixture[str], entry: str
) -> None:
    """A reason is the deliverable: an entry without one is not a valid entry."""
    _write(
        tree / "src/bernstein/core/security/listed_control.py",
        '''
        """Listed."""


        def enforce_listed_control() -> bool:
            return True
        ''',
    )
    (tree / "unreachable_controls_allowlist.txt").write_text(entry + "\n", encoding="utf-8")

    assert _run(check_module, tree) == 1
    assert "has no reason" in capsys.readouterr().err


def test_allowlist_entry_for_a_reachable_symbol_is_reported_as_stale(
    check_module: ModuleType, tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Wiring a symbol up must retire its entry, so the list cannot rot."""
    _write(
        tree / "src/bernstein/core/security/live_control.py",
        '''
        """Live."""


        def enforce_live_control() -> bool:
            return True
        ''',
    )
    _write(
        tree / "src/bernstein/core/orchestration/runner.py",
        '''
        """Runner."""

        from bernstein.core.security.live_control import enforce_live_control


        def run() -> bool:
            return enforce_live_control()
        ''',
    )
    _write(
        tree / "unreachable_controls_allowlist.txt",
        """
        src/bernstein/core/security/live_control.py::enforce_live_control  # planned wiring
        """,
    )

    assert _run(check_module, tree) == 1
    assert "now reachable or gone" in capsys.readouterr().err


def test_update_writes_a_reason_marker_the_gate_rejects(check_module: ModuleType, tree: Path) -> None:
    """``--update`` records the finding but refuses to grant it a reason."""
    _write(
        tree / "src/bernstein/core/security/new_control.py",
        '''
        """New."""


        def enforce_new_control() -> bool:
            return True
        ''',
    )

    assert _run(check_module, tree, "--update") == 0
    written = (tree / "unreachable_controls_allowlist.txt").read_text(encoding="utf-8")
    assert "enforce_new_control" in written
    assert check_module.REASON_REQUIRED in written
    assert _run(check_module, tree) == 1


def test_repository_tree_matches_the_committed_allowlist(check_module: ModuleType) -> None:
    """The shipped allowlist covers this tree exactly - no unlisted, no stale."""
    assert check_module.main(["--repo-root", str(REPO_ROOT)]) == 0
