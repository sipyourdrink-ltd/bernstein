"""Fixture package for the registry duplicate-guard regression test.

Importing this package registers the same agent definition id twice, from
two different modules, into the same :class:`~bernstein.agents.registry.AgentRegistry`
instance. The second import must fail: see
``tests/unit/core/test_registry_duplicate_guard.py::test_duplicate_id_registration_raises_at_import``.

``shared.py`` holds the single registry instance both submodules register
into, so the failure is a genuine same-id collision rather than each
submodule quietly using its own registry.
"""

from __future__ import annotations

from tests.fixtures.duplicate_registry_package import first, second

__all__ = ["first", "second"]
