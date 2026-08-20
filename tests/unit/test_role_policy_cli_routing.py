"""An explicitly configured role CLI must survive the spawn path.

``role_model_policy.<role>.cli`` (and the per-step ``cli:`` field on a task)
is an operator naming the adapter for a role. The spawn path resolves that
selection once, then re-infers the adapter a second time from the
provider/model *text* -- and the second lookup is what actually decides
which binary runs. When the second lookup disagrees with the first, the
operator's choice is discarded silently and the run dies further downstream
with an error naming an adapter the operator never asked for.

The tests below are numbered by the property each one protects:

1. An explicitly configured ``cli`` is honoured at spawn even when the
   provider-alias table (built from adapters' ``provides`` declarations)
   has no entry for it. Most registered adapters declare no aliases at all.
2. A configured ``cli`` that cannot be honoured produces a structured
   refusal naming that ``cli`` -- never a silent substitution of a
   different adapter.
3. A model string never routes across an explicit provider boundary: with
   ``cli: ollama`` a model named ``qwen2.5:7b`` must not be handed to the
   Qwen CLI because the text happens to contain another adapter's alias.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from bernstein.core.models import ModelConfig, Task
from bernstein.core.spawner import AgentSpawner

from bernstein.adapters import registry
from bernstein.core.agents.spawn_errors import AdapterNotConfiguredError

# The reported configuration (issue #4134): a local Ollama model pinned for
# the manager role. ``ollama`` is a registered, selectable adapter; it simply
# declares no ``provides`` aliases, like most adapters in the catalog.
OLLAMA_ROLE_POLICY: dict[str, str] = {
    "cli": "ollama",
    "provider": "ollama",  # the seed parser mirrors `cli` onto `provider`
    "model": "deepseek-r1:1.5b",
    "effort": "low",
}


def _make_spawner(tmp_path: Path, role_policy: dict[str, str] | None = None) -> AgentSpawner:
    """Spawner whose run-level adapter is something other than the pinned CLI.

    Mirrors the reported run: ``cli: auto`` picked Claude Code as the
    run-level adapter, while the manager role pins ``cli: ollama``.
    """
    adapter = MagicMock()
    adapter.name.return_value = "test-adapter"
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir(parents=True, exist_ok=True)
    return AgentSpawner(
        adapter,
        templates_dir,
        tmp_path,
        role_model_policy={"manager": dict(role_policy)} if role_policy else None,
    )


def _manager_task(cli: str | None = None) -> Task:
    return Task(
        id="T-4134",
        title="Fix the failing test",
        description="Fix the failing test in tests/test_add.py",
        role="manager",
        cli=cli,
    )


def _model_config(model: str = "deepseek-r1:1.5b") -> ModelConfig:
    return ModelConfig(model=model, effort="low")


def _resolve(
    spawner: AgentSpawner,
    role_policy: dict[str, Any],
    *,
    model: str = "deepseek-r1:1.5b",
    task_cli: str | None = None,
) -> tuple[ModelConfig, str | None, str]:
    """Run the spawn path's routing step the way ``spawn_for_tasks`` does."""
    preferred_provider = task_cli or role_policy.get("provider")
    return spawner._resolve_routing(  # pyright: ignore[reportPrivateUsage]
        [_manager_task(cli=task_cli)],
        _model_config(model),
        role_policy,
        preferred_provider,
    )


# --- 1. an explicit `cli` is honoured even with no provider-alias entry ----


def test_configured_role_cli_is_honoured_when_registry_has_no_provider_alias(tmp_path: Path) -> None:
    """`cli: ollama` must resolve to the ollama adapter, not to the run's adapter.

    ``ollama`` is a registered adapter with no ``provides`` aliases, so the
    alias table has no entry for it. That miss must not be treated as "the
    operator said nothing" -- it is an operator selection that names a real
    adapter.
    """
    spawner = _make_spawner(tmp_path, OLLAMA_ROLE_POLICY)
    resolved = spawner._infer_adapter_name_for_provider(  # pyright: ignore[reportPrivateUsage]
        "ollama", "deepseek-r1:1.5b"
    )
    assert resolved == "ollama"


def test_registered_adapter_name_resolves_without_a_declared_provider_alias() -> None:
    """Registry level: a selectable adapter name resolves as itself.

    Only 7 of the ~49 selectable adapters declare ``provides`` aliases. Every
    other adapter name an operator may legally write in ``cli:`` has to
    resolve, or the selection is silently dropped.
    """
    for adapter_name in ("ollama", "aider", "opencode"):
        assert adapter_name in registry.selectable_adapter_names()
        assert registry.adapter_name_for_provider(adapter_name, "deepseek-r1:1.5b") == adapter_name


def test_configured_role_cli_reaches_routing_when_only_cli_is_set(tmp_path: Path) -> None:
    """A policy carrying `cli` but no `provider` still routes to that adapter.

    The seed parser mirrors ``cli`` onto ``provider``, but policies built
    programmatically (manifests, availability chains, per-step ``cli:``)
    carry only ``cli``. Routing must not drop the selection on that path.
    """
    spawner = _make_spawner(tmp_path, {"cli": "ollama", "model": "deepseek-r1:1.5b"})
    _config, provider_name, _source = _resolve(spawner, {"cli": "ollama", "model": "deepseek-r1:1.5b"})
    assert provider_name == "ollama"


# --- 2. an unhonourable `cli` refuses by name, never substitutes ----------


def test_unresolvable_configured_cli_refuses_by_name(tmp_path: Path) -> None:
    """A `cli` naming nothing resolvable must refuse, naming that `cli`.

    The failure the reporter saw named ``Claude Code`` -- an adapter they
    never configured -- because the unresolvable selection fell through to
    the run-level adapter. The refusal has to name what the operator wrote.
    """
    spawner = _make_spawner(tmp_path, {"cli": "ollama-cloud", "model": "deepseek-r1:1.5b"})
    with pytest.raises(AdapterNotConfiguredError) as excinfo:
        _resolve(spawner, {"cli": "ollama-cloud", "model": "deepseek-r1:1.5b"})
    assert "ollama-cloud" in str(excinfo.value)
    assert excinfo.value.provider == "ollama-cloud"


def test_unresolvable_configured_cli_does_not_substitute_the_run_adapter(tmp_path: Path) -> None:
    """The refusal must not be reachable by falling back to another adapter."""
    spawner = _make_spawner(tmp_path, {"cli": "ollama-cloud", "model": "deepseek-r1:1.5b"})
    with pytest.raises(AdapterNotConfiguredError) as excinfo:
        _resolve(spawner, {"cli": "ollama-cloud", "model": "deepseek-r1:1.5b"})
    assert "test-adapter" not in str(excinfo.value)


def test_unresolvable_per_step_cli_refuses_by_name(tmp_path: Path) -> None:
    """The per-step `cli:` field on a task gets the same treatment."""
    spawner = _make_spawner(tmp_path)
    with pytest.raises(AdapterNotConfiguredError) as excinfo:
        _resolve(spawner, {}, task_cli="ollama-cloud")
    assert "ollama-cloud" in str(excinfo.value)


# --- 3. no substring matching across an explicit provider boundary --------


def test_model_string_never_routes_across_an_explicit_provider_boundary(tmp_path: Path) -> None:
    """`cli: ollama` + model `qwen2.5:7b` must not reach the Qwen CLI.

    The alias search used to run over the concatenated ``"<provider>
    <model>"`` text, so any model whose name contains another adapter's
    alias hijacked an explicit provider selection.
    """
    spawner = _make_spawner(tmp_path, {**OLLAMA_ROLE_POLICY, "model": "qwen2.5:7b"})
    resolved = spawner._infer_adapter_name_for_provider(  # pyright: ignore[reportPrivateUsage]
        "ollama", "qwen2.5:7b"
    )
    assert resolved == "ollama"


def test_model_text_is_not_consulted_when_a_provider_was_named() -> None:
    """Registry level: an unresolvable provider does not fall to the model text.

    ``qwen2.5:7b`` under provider ``ollama-cloud`` must report "no match" so
    the caller can refuse by name, rather than reporting the Qwen adapter
    the operator never selected.
    """
    assert registry.adapter_name_for_provider("ollama-cloud", "qwen2.5:7b") is None


# --- guards on the narrowing above ----------------------------------------
#
# These two hold before the fix as well as after it. They are here because
# the fix narrows two things (which values count as an adapter selection,
# and when the model text may be consulted) and each narrowing has an
# obvious way to overshoot.


def test_auto_cli_is_not_an_adapter_selection(tmp_path: Path) -> None:
    """`cli: auto` is the auto-detection sentinel, not an adapter name.

    Overshoot guard: refusing every unresolvable ``cli`` must not refuse
    ``auto``, which no adapter is registered under.
    """
    spawner = _make_spawner(tmp_path)
    _config, provider_name, _source = _resolve(spawner, {"cli": "auto"})
    assert provider_name is None


def test_model_only_inference_still_works_without_a_provider() -> None:
    """Call sites with no provider at all keep inferring from the model text.

    Overshoot guard: the sampling-capability probe passes
    ``provider_name=None`` and has only a bare model string to go on.
    Closing the provider path must not close that one.
    """
    assert registry.adapter_name_for_provider(None, "qwen3-coder") == "qwen"
    assert registry.adapter_name_for_provider(None, "gpt-5.5") == "codex"
