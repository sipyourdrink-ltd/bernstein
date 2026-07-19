"""Adapter registry - look up CLI adapters by name."""

from __future__ import annotations

import inspect
import logging
from importlib.metadata import entry_points
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

from bernstein.adapters.agy import AgyAdapter
from bernstein.adapters.aichat import AIChatAdapter
from bernstein.adapters.aider import AiderAdapter
from bernstein.adapters.amp import AmpAdapter
from bernstein.adapters.auggie import AuggieAdapter
from bernstein.adapters.autohand import AutohandAdapter
from bernstein.adapters.base import CLIAdapter
from bernstein.adapters.capability_profile import profile_built_adapter_classes
from bernstein.adapters.charm import CharmAdapter
from bernstein.adapters.claude import ClaudeCodeAdapter
from bernstein.adapters.cline import ClineAdapter
from bernstein.adapters.clm import ClmAdapter
from bernstein.adapters.cloudflare_agents import CloudflareAgentsAdapter
from bernstein.adapters.codebuff import CodebuffAdapter
from bernstein.adapters.codex import CodexAdapter
from bernstein.adapters.cody import CodyAdapter
from bernstein.adapters.composio import ComposioAdapter
from bernstein.adapters.computer_use import ReferenceComputerUseAdapter
from bernstein.adapters.continue_dev import ContinueDevAdapter
from bernstein.adapters.copilot import CopilotAdapter
from bernstein.adapters.cursor import CursorAdapter
from bernstein.adapters.devin_terminal import DevinTerminalAdapter
from bernstein.adapters.droid import DroidAdapter
from bernstein.adapters.forge import ForgeAdapter
from bernstein.adapters.gemini import GeminiAdapter
from bernstein.adapters.generic import GenericAdapter
from bernstein.adapters.goose import GooseAdapter
from bernstein.adapters.gptme import GptmeAdapter
from bernstein.adapters.hermes import HermesAdapter
from bernstein.adapters.iac import IaCAdapter
from bernstein.adapters.junie import JunieAdapter
from bernstein.adapters.kilo import KiloAdapter
from bernstein.adapters.kimi import KimiAdapter
from bernstein.adapters.kiro import KiroAdapter
from bernstein.adapters.letta_code import LettaCodeAdapter
from bernstein.adapters.mistral import MistralAdapter
from bernstein.adapters.mock import MockAgentAdapter
from bernstein.adapters.ollama import OllamaAdapter
from bernstein.adapters.open_interpreter import OpenInterpreterAdapter
from bernstein.adapters.openai_agents import OpenAIAgentsAdapter
from bernstein.adapters.opencode import OpenCodeAdapter
from bernstein.adapters.openhands import OpenHandsAdapter
from bernstein.adapters.pi import PiAdapter
from bernstein.adapters.plandex import PlandexAdapter
from bernstein.adapters.q_dev import QDevAdapter
from bernstein.adapters.qwen import QwenAdapter
from bernstein.adapters.ralphex import RalphexAdapter
from bernstein.adapters.rovo import RovoAdapter

logger = logging.getLogger(__name__)

_ADAPTERS: dict[str, type[CLIAdapter] | CLIAdapter] = {
    # Successor CLI for the discontinued non-enterprise hosted gemini
    # backend. Separate registry entry from "gemini"/"antigravity" (which
    # stay on the dual-binary GeminiAdapter for the enterprise / API-key
    # lane); see docs/adapters/agy.md for the split.
    "agy": AgyAdapter,
    "aichat": AIChatAdapter,
    "aider": AiderAdapter,
    "amp": AmpAdapter,
    "auggie": AuggieAdapter,
    "autohand": AutohandAdapter,
    "charm": CharmAdapter,
    "claude": ClaudeCodeAdapter,
    "cline": ClineAdapter,
    "clm": ClmAdapter,
    "cloudflare": CloudflareAgentsAdapter,
    "codebuff": CodebuffAdapter,
    "codex": CodexAdapter,
    "cody": CodyAdapter,
    "composio": ComposioAdapter,
    # Fronts a third-party autonomous browser / computer-use agent; per-action
    # lineage anchoring lives in the boundary layer (issue #2606).
    "computer_use": ReferenceComputerUseAdapter,
    "continue": ContinueDevAdapter,
    "copilot": CopilotAdapter,
    "cursor": CursorAdapter,
    "devin_terminal": DevinTerminalAdapter,
    "droid": DroidAdapter,
    "forge": ForgeAdapter,
    # The Google CLI ships under two binary names during the 2026-06-18
    # transition. Both registry keys resolve to the same dual-binary aware
    # adapter; the adapter discovers ``antigravity`` first on PATH and falls
    # back to ``gemini`` (or honours BERNSTEIN_GEMINI_BINARY) at spawn time.
    "antigravity": GeminiAdapter,
    "gemini": GeminiAdapter,
    "generic": GenericAdapter,
    "goose": GooseAdapter,
    "gptme": GptmeAdapter,
    "hermes": HermesAdapter,
    "iac": IaCAdapter,
    "junie": JunieAdapter,
    "kilo": KiloAdapter,
    "kimi": KimiAdapter,
    "kiro": KiroAdapter,
    "letta_code": LettaCodeAdapter,
    "mistral": MistralAdapter,
    "mock": MockAgentAdapter,
    "ollama": OllamaAdapter,
    "open_interpreter": OpenInterpreterAdapter,
    "openai_agents": OpenAIAgentsAdapter,
    "opencode": OpenCodeAdapter,
    "openhands": OpenHandsAdapter,
    "pi": PiAdapter,
    "plandex": PlandexAdapter,
    "q_dev": QDevAdapter,
    "qwen": QwenAdapter,
    "ralphex": RalphexAdapter,
    "rovo": RovoAdapter,
}

# Agents tracked as declarative capability profiles rather than as
# hand-written modules (see
# :mod:`bernstein.adapters.capability_profile`). The factory returns one
# generated CLIAdapter subclass per profile, so a profile-built agent is
# an ordinary registry citizen: it resolves through ``get_adapter``, is
# enumerated by ``iter_adapter_specs``, and faces the same conformance
# suite, strategy declaration and contract gate as a hand-written
# adapter. Declaration-only profiles are not merged here - their
# hand-written module keeps owning the spawn path.
_ADAPTERS.update(profile_built_adapter_classes())

_entrypoints_loaded = False

#: provider-alias (lower-cased) -> adapter registry name. Built lazily from
#: each adapter's ``provides`` class attribute the first time
#: :func:`adapter_name_for_provider` is called. This is the structural
#: replacement for the old hand-ordered substring `if`/`elif` chain that
#: used to live in ``spawner_core._infer_adapter_name_for_provider`` -- see
#: PR1 of the provider/adapter routing fix ladder.
_PROVIDER_ALIAS_TABLE: dict[str, str] = {}
_provider_aliases_built = False

#: Substring-fallback exclusions, keyed by alias. If the alias matches as a
#: substring of the combined provider/model text but the text ALSO contains
#: one of the excluded substrings, the match is rejected -- e.g. the bare
#: "gpt" alias (registered by the Codex adapter's ``provides``) would
#: otherwise substring-match "gpt-oss:20b", misrouting that distinct model
#: family to Codex even though it is not an OpenAI GPT model.
_ALIAS_SUBSTRING_EXCLUSIONS: dict[str, tuple[str, ...]] = {
    "gpt": ("gpt-oss",),
}


def _load_entrypoint_adapters() -> None:
    """Discover and register adapters from the ``bernstein.adapters`` entry-point group.

    Called once on first use. Silently skips malformed plugins.
    """
    global _entrypoints_loaded
    if _entrypoints_loaded:
        return
    _entrypoints_loaded = True
    for ep in entry_points(group="bernstein.adapters"):
        try:
            loaded = ep.load()
            name = ep.name
            if (inspect.isclass(loaded) and issubclass(loaded, CLIAdapter)) or isinstance(loaded, CLIAdapter):
                _ADAPTERS[name] = loaded
            else:
                logger.warning(
                    "Ignoring entry-point adapter %r: expected CLIAdapter subclass or instance, got %r",
                    name,
                    loaded,
                )
        except Exception as exc:  # pragma: no cover
            logger.warning("Failed to load entry-point adapter %r: %s", ep.name, exc)


def get_adapter(cli_name: str) -> CLIAdapter:
    """Get adapter by name, e.g. 'aider', 'claude', 'cody', 'codex', 'continue', 'gemini', or 'generic'.

    For 'generic', returns a GenericAdapter with default settings.
    For known adapters, instantiates the corresponding class.
    Third-party adapters are discovered from the ``bernstein.adapters`` entry-point group.

    Args:
        cli_name: Adapter name to look up.

    Returns:
        An instantiated CLIAdapter.

    Raises:
        ValueError: If the adapter name is not recognized.
    """
    if cli_name == "generic":
        return GenericAdapter(cli_command="generic-cli", display_name="Generic CLI")

    _load_entrypoint_adapters()

    adapter_cls = _ADAPTERS.get(cli_name)
    if adapter_cls is None:
        available = ", ".join(sorted([*_ADAPTERS.keys(), "generic"]))
        raise ValueError(f"Unknown adapter '{cli_name}'. Available: {available}")

    if isinstance(adapter_cls, CLIAdapter):
        return adapter_cls
    return adapter_cls()


def registry_name_for(adapter: CLIAdapter) -> str | None:
    """Return the registry key an adapter instance is registered under.

    Resolves the canonical registry name (e.g. ``"claude"``) for a live
    adapter so callers can key per-adapter tables (such as the strategy
    matrix in :mod:`bernstein.adapters._contract`) without each adapter
    having to restate its own key.

    Resolution prefers an explicit :attr:`CLIAdapter.registry_name`, then
    falls back to a reverse lookup over the registered classes/instances.
    Entry-point discovery runs first so third-party adapters resolve too.

    Args:
        adapter: A live :class:`CLIAdapter` instance.

    Returns:
        The registry key, or ``None`` when the adapter is not registered.
    """
    explicit = getattr(adapter, "registry_name", "") or ""
    if explicit and explicit in _ADAPTERS:
        return explicit

    _load_entrypoint_adapters()
    adapter_type = type(adapter)
    for name, entry in _ADAPTERS.items():
        if entry is adapter:
            return name
        if inspect.isclass(entry) and entry is adapter_type:
            return name
    return None


def register_adapter(name: str, adapter: type[CLIAdapter] | CLIAdapter) -> None:
    """Register a custom adapter by name.

    Args:
        name: Name to register under.
        adapter: Adapter class or instance.
    """
    _ADAPTERS[name] = adapter


def iter_adapter_specs() -> Iterator[tuple[str, type[CLIAdapter] | CLIAdapter]]:
    """Yield every registered adapter as ``(name, class-or-instance)`` pairs.

    The iterator triggers entry-point discovery on first use so third-
    party adapters are surfaced alongside the built-ins. Pairs are
    emitted in alphabetic order by name so downstream consumers can
    rely on a deterministic enumeration.

    The values are the raw registry entries (either a class or a
    pre-built instance). Callers that need a live adapter should pass
    each one through :func:`get_adapter` or instantiate themselves.
    """
    _load_entrypoint_adapters()
    for name in sorted(_ADAPTERS.keys()):
        yield name, _ADAPTERS[name]


def _register_provider_alias(alias: str, adapter_name: str) -> None:
    """Register a single provider-name alias into the provider alias table.

    Raises ``ValueError`` immediately if ``alias`` is already claimed by a
    *different* adapter. This is PR1's collision policy: an ambiguous alias
    fails loudly at table-build time (import/first-use time), never
    silently at spawn time. This is what actually forecloses the
    042bcbd0 class of bug (``openai_agents`` silently swallowed by a
    broader ``openai`` substring match) for good -- a future adapter that
    tries to claim an alias someone else already owns cannot ship at all.

    Args:
        alias: Lower-cased provider-name alias, e.g. ``"openai_agents"``.
        adapter_name: Registry name of the adapter claiming the alias, e.g.
            ``"openai_agents"``.

    Raises:
        ValueError: If ``alias`` is already registered to a different
            adapter name.
    """
    existing = _PROVIDER_ALIAS_TABLE.get(alias)
    if existing is not None and existing != adapter_name:
        raise ValueError(
            f"Provider alias {alias!r} is claimed by both adapter {existing!r} "
            f"and adapter {adapter_name!r}. Provider aliases (the `provides` "
            "class attribute on each CLIAdapter) must be unique across the "
            "whole adapter catalog -- rename or narrow one of the "
            "conflicting `provides` entries before this can load."
        )
    _PROVIDER_ALIAS_TABLE[alias] = adapter_name
    logger.debug("registry: registered provider alias %r -> adapter %r", alias, adapter_name)


def _build_provider_alias_table() -> None:
    """Populate :data:`_PROVIDER_ALIAS_TABLE` from every adapter's ``provides``.

    Idempotent: runs once, guarded by :data:`_provider_aliases_built`.
    Entry-point adapters are discovered first so third-party adapters'
    ``provides`` declarations participate in collision detection too.

    Some adapters are registered under more than one ``_ADAPTERS`` key for
    the *same* underlying class (for example ``GeminiAdapter`` is keyed as
    both ``"gemini"`` and ``"antigravity"`` during the 2026-06-18
    dual-binary transition -- see the comment in ``_ADAPTERS`` above). Each
    distinct adapter object/class must contribute its ``provides`` aliases
    exactly once, or the second registration would collide with the first
    under a *different* adapter name and trip the collision guard on a
    false positive. Entries are grouped by identity first; when a group
    has more than one registry key, the key that is itself one of the
    declared aliases (self-referencing / canonical, e.g. ``"gemini"``) is
    preferred, matching what the old hardcoded substring branch used to
    return literally.
    """
    global _provider_aliases_built
    if _provider_aliases_built:
        return
    logger.info("registry: building provider alias table from %d registered adapters", len(_ADAPTERS))
    _load_entrypoint_adapters()

    names_by_entry_id: dict[int, list[str]] = {}
    entry_by_id: dict[int, type[CLIAdapter] | CLIAdapter] = {}
    for name, entry in _ADAPTERS.items():
        eid = id(entry)
        names_by_entry_id.setdefault(eid, []).append(name)
        entry_by_id[eid] = entry

    for eid, names in names_by_entry_id.items():
        entry = entry_by_id[eid]
        provides = getattr(entry, "provides", ()) or ()
        if not provides:
            continue
        canonical = next((n for n in names if n in provides), names[0])
        if len(names) > 1:
            logger.debug(
                "registry: adapter registered under multiple keys %s; using canonical name %r for provides=%s",
                names,
                canonical,
                provides,
            )
        for alias in provides:
            _register_provider_alias(alias.strip().lower(), canonical)
    _provider_aliases_built = True
    logger.info(
        "registry: provider alias table built -- %d aliases across %d adapters: %s",
        len(_PROVIDER_ALIAS_TABLE),
        len({v for v in _PROVIDER_ALIAS_TABLE.values()}),
        _PROVIDER_ALIAS_TABLE,
    )


def adapter_name_for_provider(provider_name: str | None, model: str) -> str | None:
    """Resolve an adapter registry name from a provider name and/or model string.

    This is the structural replacement for the old
    ``spawner_core._infer_adapter_name_for_provider`` substring `if`/`elif`
    chain (Root Cause A: no canonical provider -> adapter table, so any new
    provider name that happened to be a substring of another's would
    misroute silently until someone noticed and reordered the branches).

    Resolution order:

    1. **Exact match.** ``provider_name`` (case-insensitive, stripped) is
       looked up directly against the alias table built from every
       adapter's ``provides`` declaration. Because
       :func:`_register_provider_alias` refuses ambiguous aliases at
       build time, an exact hit here is guaranteed unambiguous.
    2. **Substring fallback, longest-alias-first.** Some call sites (for
       example the sampling-capability probe in ``spawner_core.py``, which
       calls this with ``provider_name=None``) only have a bare model
       string like ``"gpt-4.1"`` or ``"claude-opus-4"`` to go on. For that
       case every registered alias is checked as a substring of
       ``f"{provider_name} {model}"``, longest alias first. Longest-first
       guarantees a more specific alias (``"openai_agents"``) always wins
       over a shorter alias it textually contains (``"openai"``) without
       requiring any hand-maintained ordering -- this is what forecloses
       the 042bcbd0 bug class structurally rather than only patching the
       one instance of it.

    Returns:
        The resolved adapter registry name, or ``None`` if nothing in the
        alias table matches. Callers apply their own fallback (typically
        the spawner's currently-active adapter) when ``None`` is returned.
    """
    logger.debug("registry.adapter_name_for_provider: provider_name=%r model=%r", provider_name, model)
    _build_provider_alias_table()

    if provider_name:
        exact = _PROVIDER_ALIAS_TABLE.get(provider_name.strip().lower())
        if exact is not None:
            logger.info(
                "registry.adapter_name_for_provider: EXACT match provider_name=%r -> adapter=%r",
                provider_name,
                exact,
            )
            return exact

    text = f"{provider_name or ''} {model}".lower()
    for alias in sorted(_PROVIDER_ALIAS_TABLE, key=len, reverse=True):
        if alias in text:
            excluded = _ALIAS_SUBSTRING_EXCLUSIONS.get(alias, ())
            if any(excl in text for excl in excluded):
                logger.info(
                    "registry.adapter_name_for_provider: SUBSTRING fallback SKIPPED "
                    "alias=%r in text=%r -- text matches an exclusion pattern for "
                    "this alias (e.g. 'gpt-oss' is a distinct model family, not an "
                    "OpenAI GPT model, despite containing the substring 'gpt')",
                    alias,
                    text,
                )
                continue
            adapter_name = _PROVIDER_ALIAS_TABLE[alias]
            logger.info(
                "registry.adapter_name_for_provider: SUBSTRING fallback matched "
                "alias=%r in text=%r -> adapter=%r (provider_name=%r had no exact hit)",
                alias,
                text,
                adapter_name,
                provider_name,
            )
            return adapter_name

    logger.info(
        "registry.adapter_name_for_provider: NO MATCH for provider_name=%r model=%r; "
        "caller must apply its own fallback",
        provider_name,
        model,
    )
    return None
