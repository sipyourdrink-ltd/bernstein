"""Tests for built-in CI log parser registration (audit-031).

Without these guarantees, ``bernstein ci fix --parser gitlab_ci`` and the
self-healing CI pipeline would silently no-op because the registry would
be empty at runtime.
"""

from __future__ import annotations

import importlib

import pytest

from bernstein.core import ci_log_parser


@pytest.fixture
def _fresh_registry(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Reset the CI parser registry to a clean state for the test.

    The registry is a module-level singleton, so we replace its internal
    dict with an empty one and restore it after the test via monkeypatch's
    teardown.
    """
    empty: dict[str, object] = {}
    monkeypatch.setattr(ci_log_parser, "_PARSERS", empty)
    return empty


def test_register_built_in_ci_parsers_populates_registry(
    _fresh_registry: dict[str, object],
) -> None:
    """Calling ``register_built_in_ci_parsers`` registers both built-ins."""
    # Re-import the module with a flag reset so register runs again against
    # the freshly-empty registry.
    from bernstein.adapters import ci as ci_pkg

    ci_pkg._BUILTINS_REGISTERED = False
    ci_pkg.register_built_in_ci_parsers()

    names = ci_log_parser.list_parsers()
    assert "github_actions" in names
    assert "gitlab_ci" in names


def test_importing_ci_package_registers_built_ins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Importing ``bernstein.adapters.ci`` alone populates the registry.

    This is the critical invariant that audit-031 fixes: any code path that
    touches the CI adapter package (for example, ``bernstein ci fix``)
    should find the built-in parsers without an explicit bootstrap call.
    """
    # Clear the registry and force a fresh import of the adapter package so
    # the module-level ``register_built_in_ci_parsers()`` side-effect runs.
    monkeypatch.setattr(ci_log_parser, "_PARSERS", {})

    import bernstein.adapters.ci as ci_pkg

    ci_pkg._BUILTINS_REGISTERED = False
    importlib.reload(ci_pkg)

    names = ci_log_parser.list_parsers()
    assert "github_actions" in names
    assert "gitlab_ci" in names


def test_ci_parsers_discoverable_by_name(_fresh_registry: dict[str, object]) -> None:
    """Each registered parser is retrievable by its canonical name."""
    from bernstein.adapters import ci as ci_pkg
    from bernstein.adapters.ci.github_actions import GitHubActionsParser
    from bernstein.adapters.ci.gitlab_ci import GitLabCIParser

    ci_pkg._BUILTINS_REGISTERED = False
    ci_pkg.register_built_in_ci_parsers()

    gha = ci_log_parser.get_parser("github_actions")
    gitlab = ci_log_parser.get_parser("gitlab_ci")

    assert isinstance(gha, GitHubActionsParser)
    assert isinstance(gitlab, GitLabCIParser)
    assert gha.name == "github_actions"
    assert gitlab.name == "gitlab_ci"


def test_register_built_in_ci_parsers_is_idempotent(
    _fresh_registry: dict[str, object],
) -> None:
    """Calling the registrar twice does not duplicate or error."""
    from bernstein.adapters import ci as ci_pkg

    ci_pkg._BUILTINS_REGISTERED = False
    ci_pkg.register_built_in_ci_parsers()
    first = set(ci_log_parser.list_parsers())

    ci_pkg.register_built_in_ci_parsers()  # second call — must be a no-op
    second = set(ci_log_parser.list_parsers())

    assert first == second
    assert {"github_actions", "gitlab_ci"}.issubset(second)


def test_bootstrap_registers_ci_parsers(monkeypatch: pytest.MonkeyPatch) -> None:
    """The orchestrator bootstrap helper populates the registry."""
    monkeypatch.setattr(ci_log_parser, "_PARSERS", {})

    from bernstein.adapters import ci as ci_pkg

    ci_pkg._BUILTINS_REGISTERED = False

    from bernstein.core.orchestration.bootstrap import _register_ci_parsers

    _register_ci_parsers()

    names = ci_log_parser.list_parsers()
    assert "github_actions" in names
    assert "gitlab_ci" in names
