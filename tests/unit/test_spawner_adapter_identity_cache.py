"""The run-level adapter instance survives a registry-key lookup (#5348).

``_infer_adapter_name_for_provider`` resolves to the *registry key* so the
contract, receipt, and capability-profile lookups address the files that
actually exist (``qwen.yaml``, not ``Qwen CLI.yaml``). ``_adapter_cache``,
however, was seeded only under the adapter's *display name*, and 44 of the 53
registered adapters have a display name that is not their key. Every
registry-key lookup therefore missed, and the miss path in
``_get_adapter_by_name`` built a second instance out of the registry: the
run-level instance the caller injected was silently dropped, together with its
host-isolation declaration and its ``CachingAdapter`` wrap. An injected
adapter that is not registered at all -- a test double, a third-party adapter
-- did not merely lose its identity, it failed the spawn.

The resolution is by identity (``registry_name_for``), never by folding a
display name back to a key. ``AgyAdapter`` displays as "Antigravity" while
``antigravity`` is a registry alias for ``GeminiAdapter``, so a name-string
fold silently lands an ``agy`` spawn on the Gemini adapter -- a different
vendor CLI, a different binary, a different contract.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from bernstein.core.spawner import AgentSpawner

from bernstein.adapters.agy import AgyAdapter
from bernstein.adapters.gemini import GeminiAdapter
from bernstein.adapters.registry import get_adapter, registry_name_for


def _spawner(tmp_path: Path, adapter: object, *, enable_caching: bool = False) -> AgentSpawner:
    templates_dir = tmp_path / "templates" / "roles"
    templates_dir.mkdir(parents=True, exist_ok=True)
    return AgentSpawner(adapter, templates_dir, tmp_path, enable_caching=enable_caching)  # type: ignore[arg-type]


def test_registry_key_lookup_returns_the_injected_run_level_adapter(tmp_path: Path) -> None:
    """The instance the caller handed in is what the spawn path gets back."""
    adapter = AgyAdapter()
    spawner = _spawner(tmp_path, adapter)

    # The name the spawn path actually asks for on the fallback path.
    assert spawner._infer_adapter_name_for_provider(None, "some-proxy/model") == "agy"
    assert spawner._get_adapter_by_name("agy") is adapter
    # The display name keeps working for older call sites.
    assert spawner._get_adapter_by_name("Antigravity") is adapter


def test_registry_key_lookup_survives_the_caching_wrap(tmp_path: Path) -> None:
    """``CachingAdapter`` is not itself registered, so identity is resolved
    before the wrap. Resolving after it reports a registered adapter as
    unregistered and falls back to the display name -- reinstating #5348."""
    adapter = AgyAdapter()
    spawner = _spawner(tmp_path, adapter, enable_caching=True)

    assert spawner._infer_adapter_name_for_provider(None, "some-proxy/model") == "agy"
    wrapped = spawner._get_adapter_by_name("agy")
    assert wrapped is spawner._adapter
    assert getattr(wrapped, "_inner", None) is adapter


def test_unregistered_injected_adapter_is_still_reachable(tmp_path: Path) -> None:
    """A test double or third-party adapter has no registry key. It must keep
    resolving by display name rather than failing the spawn."""
    adapter = MagicMock()
    adapter.name.return_value = "Third Party Agent"
    spawner = _spawner(tmp_path, adapter)

    assert spawner._adapter_registry_name is None
    assert spawner._infer_adapter_name_for_provider(None, "some-proxy/model") == "Third Party Agent"
    assert spawner._get_adapter_by_name("Third Party Agent") is adapter


def test_agy_display_name_collides_with_the_antigravity_registry_key() -> None:
    """The collision that makes any display-name-to-key string fold unsound.

    ``agy``'s display name is exactly another adapter's registry key. Folding
    the string "Antigravity" to a key resolves ``antigravity`` -> the Gemini
    adapter, so an ``agy`` spawn would receive a different vendor's CLI.
    Identity resolution is what keeps the two apart; this test fails the moment
    someone reintroduces a name-string map.
    """
    agy = AgyAdapter()
    assert agy.name() == "Antigravity"
    assert isinstance(get_adapter("antigravity"), GeminiAdapter)
    assert not isinstance(get_adapter("antigravity"), AgyAdapter)

    # Identity, not the name string, is what resolves the key.
    assert registry_name_for(agy) == "agy"
