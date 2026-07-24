"""Registry-shape tests for ``bernstein.adapters.registry``.

Locks the public count narrative: ``bernstein adapters list`` and the
README must agree that 44 adapters ship today, with ``generic`` being one
of the 44 (registered in ``_ADAPTERS`` rather than special-cased only).
"""

from __future__ import annotations

import pytest

from bernstein.adapters.generic import GenericAdapter
from bernstein.adapters.registry import (
    _ADAPTERS,
    _REMOVED_ADAPTERS,
    get_adapter,
    removed_adapter_message,
    selectable_adapter_names,
)


def test_generic_in_adapters_registry() -> None:
    """``generic`` must be a first-class entry in ``_ADAPTERS``.

    The ``bernstein adapters list`` command enumerates ``_ADAPTERS``; if
    ``generic`` is only served by the special-case branch in
    ``get_adapter``, it is invisible to the listing command and the
    README's adapter count drifts.
    """
    assert "generic" in _ADAPTERS


def test_adapter_count_at_least_44() -> None:
    """Lock the public adapter count cited in README / landing copy.

    Source of truth: ``len(_ADAPTERS)`` (also surfaced by
    ``bernstein adapters list``). If you add or remove an adapter, update
    README.md (lines for ``CLI agent adapters`` and the comparison tables)
    so the public count stays honest.
    """
    assert len(_ADAPTERS) >= 44, sorted(_ADAPTERS)


def test_get_adapter_generic_returns_generic_adapter() -> None:
    """``get_adapter('generic')`` must still resolve to a ``GenericAdapter``.

    Registry-dict registration must not break the existing special-case
    in ``get_adapter`` that returns a pre-configured GenericAdapter.
    """
    adapter = get_adapter("generic")
    assert isinstance(adapter, GenericAdapter)


# ---------------------------------------------------------------------------
# Removed adapters (issue #2970)
# ---------------------------------------------------------------------------


def test_removed_names_are_not_registered_or_selectable() -> None:
    """A removed name must not reappear in the registry or any selection surface."""
    for name in _REMOVED_ADAPTERS:
        assert name not in _ADAPTERS, f"{name} is listed as removed but still registered"
        assert name not in selectable_adapter_names()


def test_removed_adapter_message_only_covers_removed_names() -> None:
    """Unknown names are plain typos and get no replacement guidance."""
    assert removed_adapter_message("this-adapter-never-existed") is None
    for name in _REMOVED_ADAPTERS:
        assert removed_adapter_message(name)


def test_get_adapter_on_removed_name_names_the_replacement() -> None:
    """Resolving a removed adapter points at the supported path, not a bare failure.

    The failure mode this guards is an operator whose config still pins
    ``cloudflare``: they must get a pointer rather than an ``ImportError``
    from a module that no longer exists or an "Unknown adapter" list they
    have to interpret themselves.
    """
    with pytest.raises(ValueError) as excinfo:
        get_adapter("cloudflare")
    msg = str(excinfo.value)
    assert "has been removed" in msg
    assert "codex_cloudflare" in msg
    assert "CloudflareBridge" in msg
    assert "Unknown adapter" not in msg


def test_get_adapter_on_unknown_name_still_lists_available() -> None:
    """A typo keeps the generic listing; only removed names get guidance."""
    with pytest.raises(ValueError, match="Unknown adapter"):
        get_adapter("this-adapter-never-existed")
