"""No two conftests may register the same Hypothesis profile with conflicting kwargs.

``hypothesis.settings.register_profile`` keys its registry by name, globally
within a process. ``tests/property/conftest.py`` and ``tests/unit/conftest.py``
each register a profile named ``"deep"`` -- with, until this fix, conflicting
``derandomize`` values (``False`` in the former, ``True`` in the latter,
introduced by #4128 copy-pasting the ``smoke`` profile's ``derandomize=True``
onto ``deep`` as well).

Today the two conftests are not both imported in the same pytest process in
CI: ``nightly-deep-tests.yml``'s ``hypothesis-deep`` job scopes its run to
``tests/property/``, and ``scripts/run_tests.py`` defaults to ``tests/unit``
only. But that separation lives in CI workflow YAML and a script default, not
in anything this test file enforces, so a future change that runs
``pytest tests/`` from the repo root -- in CI or on a developer's laptop --
would silently pick whichever file's registration happened to import last,
with no test catching the regression. This test reads every conftest's
source directly, so it needs no such invocation to fail: it is order- and
CI-topology-independent by construction.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _registered_profiles(source: str) -> dict[str, dict[str, object]]:
    """Map profile name -> {kwarg: literal value} for every ``register_profile`` call.

    Only calls whose name argument and kwarg values are literals are
    recorded; a computed value cannot be compared statically, and pretending
    otherwise would make this test lie rather than fail.
    """
    profiles: dict[str, dict[str, object]] = {}
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "register_profile"):
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant):
            continue
        name = node.args[0].value
        if not isinstance(name, str):
            continue
        kwargs = {
            kw.arg: kw.value.value for kw in node.keywords if kw.arg is not None and isinstance(kw.value, ast.Constant)
        }
        profiles[name] = kwargs
    return profiles


def _conftests() -> list[Path]:
    return sorted(p for p in (REPO_ROOT / "tests").rglob("conftest.py") if p.is_file())


def test_same_named_profile_has_identical_settings_across_conftests() -> None:
    by_name: dict[str, list[tuple[Path, dict[str, object]]]] = {}
    for conftest in _conftests():
        for name, kwargs in _registered_profiles(conftest.read_text(encoding="utf-8")).items():
            by_name.setdefault(name, []).append((conftest, kwargs))

    offenders: list[str] = []
    for name, registrations in by_name.items():
        if len(registrations) < 2:
            continue
        _, first_kwargs = registrations[0]
        for conftest, kwargs in registrations[1:]:
            shared_keys = kwargs.keys() & first_kwargs.keys()
            mismatched = {k for k in shared_keys if kwargs[k] != first_kwargs[k]}
            if mismatched:
                offenders.append(
                    f"{conftest.relative_to(REPO_ROOT)} registers {name!r} with "
                    f"{mismatched} differing from {registrations[0][0].relative_to(REPO_ROOT)}"
                )

    assert not offenders, (
        "these conftests register the same Hypothesis profile name with "
        f"conflicting values: {offenders}. register_profile() is keyed by name "
        "globally within a process, so if both files are ever imported in the "
        "same pytest run, whichever registers last silently wins -- give the "
        "profiles distinct names or make the conflicting kwargs agree."
    )


def test_the_profile_scan_actually_finds_something() -> None:
    """A scan that silently matched nothing would pass the test above forever."""
    conftests = _conftests()
    assert conftests, "no conftest.py found under tests/ -- the scan is looking in the wrong place"

    found = False
    for conftest in conftests:
        if _registered_profiles(conftest.read_text(encoding="utf-8")):
            found = True
            break
    assert found, (
        "no conftest registers a Hypothesis profile by literal name, so the "
        "agreement check above cannot fail on anything. If every conftest "
        "genuinely stopped calling register_profile with literals, delete both "
        "tests rather than keeping a check that guards nothing."
    )


def test_a_profile_registration_is_read_out_of_register_profile_source() -> None:
    """Pin the extractor itself: it reads name and kwargs, not just the call site."""
    source = (
        "from hypothesis import settings\n"
        "settings.register_profile('x', derandomize=True, max_examples=1)\n"
        "settings.register_profile(computed_name, derandomize=False)\n"
    )
    assert _registered_profiles(source) == {"x": {"derandomize": True, "max_examples": 1}}
