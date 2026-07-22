"""Unit tests for provider -> adapter name resolution.

PR1 of the provider/adapter routing fix ladder replaced the hand-ordered
substring `if`/`elif` chain that used to live directly in
``AgentSpawner._infer_adapter_name_for_provider`` with a real registry
lookup (:func:`bernstein.adapters.registry.adapter_name_for_provider`),
built from each adapter's declared ``provides`` aliases.

Original regression coverage (kept, still exercised end-to-end through
``AgentSpawner``): the bare "openai" check used to match before
"openai_agents" was considered, so every role_model_policy entry with
provider: openai_agents was misrouted to the codex adapter instead of its
own adapter (fixed structurally here, fixed as a point-fix in 042bcbd0).

New coverage added in PR1: the registry module directly, including one
assertion per currently-registered adapter alias (so a future ambiguous
alias addition fails at test time, not silently at spawn time) and the
collision-at-registration-time guarantee itself.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from bernstein.core.spawner import AgentSpawner

from bernstein.adapters import registry


def _make_spawner(tmp_path: Path) -> AgentSpawner:
    adapter = MagicMock()
    adapter.name.return_value = "test-adapter"
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir(parents=True, exist_ok=True)
    return AgentSpawner(adapter, templates_dir, tmp_path)


def _make_pinned_spawner(tmp_path: Path, adapter_name: str) -> AgentSpawner:
    """Spawner whose run-level adapter is an explicit operator pin.

    Mirrors the orchestrator construction path for ``--adapter`` /
    ``BERNSTEIN_ADAPTER`` / a non-``auto`` seed ``cli`` value, all of which
    set ``adapter_pinned=True`` on the spawner.
    """
    adapter = MagicMock()
    adapter.name.return_value = adapter_name
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir(parents=True, exist_ok=True)
    return AgentSpawner(adapter, templates_dir, tmp_path, adapter_pinned=True)


def test_openai_agents_provider_routes_to_openai_agents_adapter(tmp_path: Path) -> None:
    """provider='openai_agents' must not be swallowed by the 'openai' check."""
    spawner = _make_spawner(tmp_path)
    result = spawner._infer_adapter_name_for_provider("openai_agents", "MiniMax-M3")
    assert result == "openai_agents"


def test_bare_gpt_model_with_no_provider_routes_to_codex(tmp_path: Path) -> None:
    """provider=None + a gpt-family model name still routes to codex."""
    spawner = _make_spawner(tmp_path)
    result = spawner._infer_adapter_name_for_provider(None, "gpt-5.5")
    assert result == "codex"


def test_bare_openai_provider_routes_to_codex(tmp_path: Path) -> None:
    """provider='openai' (not openai_agents) keeps its existing codex routing."""
    spawner = _make_spawner(tmp_path)
    result = spawner._infer_adapter_name_for_provider("openai", "some-model")
    assert result == "codex"


def test_gpt_oss_model_does_not_route_to_codex(tmp_path: Path) -> None:
    """gpt-oss:20b must NOT be misrouted to Codex via the bare 'gpt' substring
    alias -- gpt-oss is a distinct open-weights model family, not an OpenAI
    GPT model, despite textually containing 'gpt'."""
    spawner = _make_spawner(tmp_path)
    result = spawner._infer_adapter_name_for_provider(None, "gpt-oss:20b")
    assert result != "codex"


def test_gpt_oss_120b_model_does_not_route_to_codex(tmp_path: Path) -> None:
    """Same exclusion applies to other gpt-oss size variants."""
    spawner = _make_spawner(tmp_path)
    result = spawner._infer_adapter_name_for_provider(None, "gpt-oss:120b")
    assert result != "codex"


def test_legitimate_gpt_model_still_routes_to_codex(tmp_path: Path) -> None:
    """Legitimate gpt-* models (not gpt-oss) must still route to codex after
    the gpt-oss exclusion is applied."""
    spawner = _make_spawner(tmp_path)
    result = spawner._infer_adapter_name_for_provider(None, "gpt-4.1")
    assert result == "codex"


def test_registry_gpt_oss_excluded_from_gpt_alias() -> None:
    """Direct registry-level coverage: the substring fallback must reject the
    'gpt' alias match when the model text contains 'gpt-oss'."""
    assert registry.adapter_name_for_provider(None, "gpt-oss:20b") != "codex"
    assert registry.adapter_name_for_provider(None, "gpt-5.5") == "codex"


def test_pinned_adapter_wins_over_model_namespace_substring(tmp_path: Path) -> None:
    """Regression (#2751): an explicit adapter pin must never be hijacked by
    model-name substring inference. With the run pinned to qwen and no
    per-spawn provider (task cli 'auto'), an OpenRouter route id like
    'openai/gpt-oss-20b:free' used to substring-match the 'openai' alias and
    misroute the spawn to codex."""
    spawner = _make_pinned_spawner(tmp_path, "qwen")
    result = spawner._infer_adapter_name_for_provider(None, "openai/gpt-oss-20b:free")
    assert result == "qwen"


def test_pinned_adapter_explicit_provider_still_wins_over_pin(tmp_path: Path) -> None:
    """A per-spawn provider selection (task `cli:` / role_model_policy
    provider) is more specific than the run-level pin and must still resolve
    to its own adapter via exact alias lookup."""
    spawner = _make_pinned_spawner(tmp_path, "qwen")
    result = spawner._infer_adapter_name_for_provider("codex", "openai/gpt-oss-20b:free")
    assert result == "codex"


def test_pinned_adapter_provider_lookup_never_consults_model_text(tmp_path: Path) -> None:
    """Under a pin, an unrecognized per-spawn provider must fall back to the
    pinned adapter - never to an adapter substring-inferred from the model
    string."""
    spawner = _make_pinned_spawner(tmp_path, "qwen")
    result = spawner._infer_adapter_name_for_provider("totally-unknown-provider", "openai/gpt-oss-20b:free")
    assert result == "qwen"


def test_unpinned_model_name_inference_is_unchanged(tmp_path: Path) -> None:
    """When nothing is pinned anywhere (adapter_pinned defaults to False),
    model-name inference keeps working exactly as before."""
    spawner = _make_spawner(tmp_path)
    assert spawner._infer_adapter_name_for_provider(None, "gpt-5.5") == "codex"
    assert spawner._infer_adapter_name_for_provider(None, "qwen3-coder") == "qwen"


def test_unrecognized_provider_falls_back_to_current_adapter(tmp_path: Path) -> None:
    """Unrecognized provider/model still falls back to self._adapter.name(),
    exactly as the old substring chain did -- Claude-only / unrecognized
    provider operators must see unchanged behavior."""
    spawner = _make_spawner(tmp_path)
    result = spawner._infer_adapter_name_for_provider("totally-unknown-provider", "totally-unknown-model")
    assert result == "test-adapter"


# --- registry.adapter_name_for_provider: direct unit coverage ---------------


def test_registry_exact_match_openai_agents() -> None:
    """Exact provider-name match: 'openai_agents' resolves to its own adapter,
    not the broader 'openai'/'codex' alias -- this is the core regression the
    registry replacement exists to make structurally impossible to reintroduce."""
    assert registry.adapter_name_for_provider("openai_agents", "MiniMax-M3") == "openai_agents"


def test_registry_substring_fallback_prefers_longest_alias() -> None:
    """provider_name=None, model text contains both 'openai' and would-be
    'openai_agents' style text: longest-alias-first must still resolve to the
    more specific adapter, matching what the old hand-ordered chain achieved
    by putting the openai_agents check first."""
    assert registry.adapter_name_for_provider(None, "openai_agents runner build") == "openai_agents"


def test_registry_unknown_provider_and_model_returns_none() -> None:
    """No match in the alias table returns None; callers apply their own
    fallback (AgentSpawner falls back to self._adapter.name())."""
    assert registry.adapter_name_for_provider("totally-unknown-provider", "totally-unknown-model") is None


def test_registry_all_registered_adapter_aliases_resolve_correctly() -> None:
    """One assertion per registered adapter alias.

    Walks the live provider-alias table (built from every adapter's
    ``provides`` declaration) and asserts each alias resolves back to its
    owning adapter by exact match. A future adapter that declares an alias
    already claimed by another adapter fails loudly at
    ``_register_provider_alias`` time (see
    ``test_registry_ambiguous_alias_raises_at_registration_time`` below)
    long before this test would even run -- this test instead guards
    against a *correct-but-wrong-target* regression, e.g. someone
    accidentally repointing an alias at the wrong adapter name.
    """
    registry._build_provider_alias_table()
    alias_table = dict(registry._PROVIDER_ALIAS_TABLE)
    assert alias_table, "expected at least one adapter to declare `provides` aliases"
    for alias, expected_adapter_name in alias_table.items():
        resolved = registry.adapter_name_for_provider(alias, "irrelevant-model")
        assert resolved == expected_adapter_name, (
            f"alias {alias!r} resolved to adapter {resolved!r}, expected {expected_adapter_name!r}"
        )


def test_registry_ambiguous_alias_raises_at_registration_time() -> None:
    """Collision policy: two adapters claiming the same alias must fail
    loudly at registration time, not silently misroute at spawn time.

    This is what actually forecloses the 042bcbd0 bug class going forward:
    it is no longer possible to ship two adapters with an overlapping
    ``provides`` alias without the table failing to build.
    """
    test_alias = "__pr1_collision_test_alias__"
    registry._register_provider_alias(test_alias, "adapter-a")
    try:
        with pytest.raises(ValueError, match="claimed by both"):
            registry._register_provider_alias(test_alias, "adapter-b")
    finally:
        # Don't leak test-only state into the shared module-level table.
        registry._PROVIDER_ALIAS_TABLE.pop(test_alias, None)


def test_registry_same_alias_same_adapter_is_not_a_collision() -> None:
    """Re-registering the identical (alias, adapter_name) pair is a no-op,
    not a collision -- this is what makes the dual-binary gemini/antigravity
    registration (same class under two ``_ADAPTERS`` keys) safe."""
    test_alias = "__pr1_idempotent_test_alias__"
    try:
        registry._register_provider_alias(test_alias, "adapter-a")
        registry._register_provider_alias(test_alias, "adapter-a")  # must not raise
        assert registry._PROVIDER_ALIAS_TABLE[test_alias] == "adapter-a"
    finally:
        registry._PROVIDER_ALIAS_TABLE.pop(test_alias, None)
