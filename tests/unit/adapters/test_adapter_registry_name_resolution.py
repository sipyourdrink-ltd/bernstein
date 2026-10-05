"""Unit tests for adapter registry_name resolution and reverse lookup (#5497).

Guarantees:
1. `get_adapter(key)` stamps `instance.registry_name = key` on the returned instance.
2. `registry_name_for` returns the selected key for instances resolved via `get_adapter`.
3. An ambiguous class registered under multiple keys returns `None` from `registry_name_for`
   when instantiated directly without stamping, rather than silently relying on dictionary
   insertion order. A class that declares its own `registry_name` (e.g. `AgyAdapter`, keyed as
   both `agy` and `antigravity`) resolves to that declared key.
4. Single-key adapters continue to resolve correctly when unstamped.
5. Admission evidence for `gemini`, `agy` and `antigravity` names their own adapter key and binary.
"""

from __future__ import annotations

from bernstein.adapters.admission import gather_admission_evidence
from bernstein.adapters.agy import AgyAdapter
from bernstein.adapters.aider import AiderAdapter
from bernstein.adapters.base import CLIAdapter
from bernstein.adapters.claude import ClaudeCodeAdapter
from bernstein.adapters.codex import CodexAdapter
from bernstein.adapters.gemini import GeminiAdapter
from bernstein.adapters.registry import (
    _ADAPTERS,
    get_adapter,
    register_adapter,
    registry_name_for,
)


def teardown_function() -> None:
    """Clean up any temporary test registrations."""
    _ADAPTERS.pop("test_dual_1", None)
    _ADAPTERS.pop("test_dual_2", None)


def test_get_adapter_stamps_registry_name() -> None:
    """get_adapter stamps the requested registry key onto the returned instance."""
    gemini = get_adapter("gemini")
    assert gemini.registry_name == "gemini"

    antigravity = get_adapter("antigravity")
    assert antigravity.registry_name == "antigravity"

    generic = get_adapter("generic")
    assert generic.registry_name == "generic"

    claude = get_adapter("claude")
    assert claude.registry_name == "claude"


def test_antigravity_key_resolves_to_agy_adapter() -> None:
    """The ``antigravity`` key is the agy adapter; ``gemini`` stays on GeminiAdapter."""
    antigravity = get_adapter("antigravity")
    assert isinstance(antigravity, AgyAdapter)
    assert registry_name_for(antigravity) == "antigravity"

    agy = get_adapter("agy")
    assert isinstance(agy, AgyAdapter)
    assert registry_name_for(agy) == "agy"

    gemini = get_adapter("gemini")
    assert isinstance(gemini, GeminiAdapter)
    assert registry_name_for(gemini) == "gemini"


def test_dual_registered_class_with_declared_key_resolves_unstamped() -> None:
    """An unstamped AgyAdapter resolves to its declared key, not by dict order."""
    bare_agy = AgyAdapter()
    assert registry_name_for(bare_agy) == "agy"


def test_gemini_resolves_when_unstamped() -> None:
    """GeminiAdapter is single-key again and resolves by class reverse-lookup."""
    assert registry_name_for(GeminiAdapter()) == "gemini"


def test_single_key_adapters_continue_to_resolve_when_unstamped() -> None:
    """Single-key adapters resolve via class reverse-lookup even without explicit stamping."""
    bare_claude = ClaudeCodeAdapter()
    assert registry_name_for(bare_claude) == "claude"

    bare_aider = AiderAdapter()
    assert registry_name_for(bare_aider) == "aider"

    bare_codex = CodexAdapter()
    assert registry_name_for(bare_codex) == "codex"


def test_multi_registered_custom_adapter_disambiguation() -> None:
    """A custom adapter registered under two keys requires stamping to disambiguate."""

    class CustomMultiAdapter(CLIAdapter):
        def spawn(self, **kwargs):  # type: ignore[no-untyped-def]
            raise NotImplementedError

        def name(self) -> str:
            return "CustomMulti"

    register_adapter("test_dual_1", CustomMultiAdapter)
    register_adapter("test_dual_2", CustomMultiAdapter)

    # Unstamped instance is ambiguous
    bare = CustomMultiAdapter()
    assert registry_name_for(bare) is None

    # Stamped via get_adapter resolves to the respective key
    inst1 = get_adapter("test_dual_1")
    assert inst1.registry_name == "test_dual_1"
    assert registry_name_for(inst1) == "test_dual_1"

    inst2 = get_adapter("test_dual_2")
    assert inst2.registry_name == "test_dual_2"
    assert registry_name_for(inst2) == "test_dual_2"


def test_admission_evidence_for_gemini_and_antigravity() -> None:
    """Admission evidence names each key; ``antigravity`` pins the ``agy`` binary."""
    ev_gemini = gather_admission_evidence("gemini")
    assert ev_gemini.adapter == "gemini"
    assert ev_gemini.binary == "gemini"
    assert ev_gemini.contract_hash != ""

    ev_antigravity = gather_admission_evidence("antigravity")
    assert ev_antigravity.adapter == "antigravity"
    assert ev_antigravity.binary == "agy"
    assert ev_antigravity.contract_hash != ""
