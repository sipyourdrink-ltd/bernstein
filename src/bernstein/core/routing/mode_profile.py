"""Per-model agent mode profiles (system prompt + tool subset + turn budget).

Bernstein chooses a model via the bandit/cascade routers based purely on
cost/quality signals. Once a model is selected, the *mode profile* layered
on top tailors the agent's interaction style to that model's personality:
- ``smart`` - Claude-family models, rapid feedback, broad tool surface.
- ``deep`` - GPT-5.2-class models, long autonomous research, narrow tools.
- ``fast`` - small/fast models, terse output, minimal tool surface.

Profiles are loaded from ``templates/mode_profiles/<name>.yaml`` at startup
when present and fall back to the in-code defaults below otherwise. The
mapping ``model_id -> default mode`` is deterministic; per-task overrides
are honoured via ``Task.metadata['mode']`` (e.g. ``mode=fast``).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from pathlib import Path

from bernstein.core.dataclass_helpers import typed_replace as _typed_replace

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModeProfile:
    """Mode-specific configuration applied after model selection.

    Attributes:
        name: Profile identifier (``smart``, ``deep``, ``fast``).
        system_prompt_preamble: Text prepended to the agent's system prompt.
        tool_subset: Allowlist of tool names; empty tuple means "all tools".
        temperature: Sampling temperature target for the adapter.
        top_p: Optional nucleus-sampling target. ``None`` leaves the
            provider default in place (the profile expresses no preference).
        top_k: Optional top-k sampling target, forwarded to
            OpenAI-compatible endpoints that accept it. ``None`` leaves the
            provider default in place.
        max_tokens: Optional per-run output-token cap. ``None`` leaves the
            adapter's own default in place.
        max_turns: Upper bound on conversation turns for this profile.
        expected_runtime_minutes: Wall-clock budget hint for callers.
    """

    name: str
    system_prompt_preamble: str
    tool_subset: tuple[str, ...] = ()
    temperature: float = 0.2
    top_p: float | None = None
    top_k: int | None = None
    max_tokens: int | None = None
    max_turns: int = 40
    expected_runtime_minutes: int = 15

    def filter_tools(self, available: list[str]) -> list[str]:
        """Return *available* tools intersected with this profile's allowlist."""
        if not self.tool_subset:
            return available.copy()
        allowed = set(self.tool_subset)
        return [t for t in available if t in allowed]

    def apply_preamble(self, prompt: str) -> str:
        """Prepend the profile preamble to *prompt* with a blank-line separator."""
        if not self.system_prompt_preamble:
            return prompt
        return f"{self.system_prompt_preamble.rstrip()}\n\n{prompt}"


_SMART_DEFAULT = ModeProfile(
    name="smart",
    system_prompt_preamble=(
        "## Mode: smart (rapid feedback)\n"
        "You operate in interactive smart mode. Make small, verifiable steps; "
        "report progress often; ask for clarification when blocked rather than "
        "guessing. Prefer concise responses and iterate quickly with the user."
    ),
    tool_subset=(),
    temperature=0.2,
    max_turns=40,
    expected_runtime_minutes=15,
)

_DEEP_DEFAULT = ModeProfile(
    name="deep",
    system_prompt_preamble=(
        "## Mode: deep (long autonomous research)\n"
        "You operate in deep autonomous mode. Plan thoroughly before acting, "
        "minimise external chatter, and only surface findings when you have a "
        "complete answer. Use tools sparingly and prefer reasoning."
    ),
    tool_subset=("Read", "Grep", "Glob", "Bash"),
    temperature=0.3,
    max_turns=120,
    expected_runtime_minutes=60,
)

_FAST_DEFAULT = ModeProfile(
    name="fast",
    system_prompt_preamble=(
        "## Mode: fast (terse, minimal tool use)\n"
        "You operate in fast mode. Produce the shortest correct answer. "
        "Avoid exploratory tool calls; prefer a single well-targeted action."
    ),
    tool_subset=("Read", "Edit"),
    temperature=0.0,
    max_turns=15,
    expected_runtime_minutes=5,
)


MODE_REGISTRY: dict[str, ModeProfile] = {
    "smart": _SMART_DEFAULT,
    "deep": _DEEP_DEFAULT,
    "fast": _FAST_DEFAULT,
}


_MODEL_FAMILY_TO_MODE: dict[str, str] = {
    "claude": "smart",
    "opus": "smart",
    "sonnet": "smart",
    "haiku": "fast",
    "gpt-5": "deep",
    "gpt-5.1": "deep",
    "gpt-5.2": "deep",
    "o1": "deep",
    "o3": "deep",
    "gemini": "smart",
    "qwen": "fast",
    "ollama": "fast",
}


def _classify_model_family(model_id: str) -> str:
    """Return the family key that maps a model id to its default mode."""
    lower = model_id.lower().strip()
    if not lower:
        return "claude"
    for prefix in (
        "gpt-5.2",
        "gpt-5.1",
        "gpt-5",
        "opus",
        "sonnet",
        "haiku",
        "claude",
        "gemini",
        "qwen",
        "ollama",
        "o3",
        "o1",
    ):
        if prefix in lower:
            return prefix
    return "claude"


def select_mode(
    model_id: str,
    task: Any | None = None,
    registry: dict[str, ModeProfile] | None = None,
) -> ModeProfile:
    """Pick the mode profile for *task* running on *model_id*.

    Resolution order:
    1. Explicit override via ``task.metadata['mode']`` (when present and known).
    2. Model-family default from :data:`_MODEL_FAMILY_TO_MODE`.
    3. ``smart`` as a final fallback.

    Args:
        model_id: Selected model identifier (e.g. ``"claude-sonnet-4-6"``).
        task: Optional task carrying a ``metadata`` dict; ``metadata['mode']``
            forces a specific profile when its value matches a registry key.
        registry: Override registry (defaults to :data:`MODE_REGISTRY`).

    Returns:
        A :class:`ModeProfile` from the registry. Always non-``None``.
    """
    table = registry if registry is not None else MODE_REGISTRY

    if task is not None:
        metadata = getattr(task, "metadata", None)
        if isinstance(metadata, Mapping):
            metadata_map = cast(Mapping[str, object], metadata)
            override = metadata_map.get("mode")
            if isinstance(override, str) and override in table:
                return table[override]

    family = _classify_model_family(model_id)
    mode_name = _MODEL_FAMILY_TO_MODE.get(family, "smart")
    return table.get(mode_name, table.get("smart", _SMART_DEFAULT))


def _coerce_profile(name: str, raw: dict[str, Any]) -> ModeProfile | None:
    """Build a :class:`ModeProfile` from a YAML-decoded dict; return ``None`` on bad input."""
    try:
        tool_subset_value = raw.get("tool_subset", [])
        if tool_subset_value is None:
            tool_subset_raw: list[object] = []
        elif isinstance(tool_subset_value, list):
            tool_subset_raw = cast(list[object], tool_subset_value)
        else:
            raise TypeError(f"tool_subset must be a list, got {type(tool_subset_value).__name__}")
        top_p_raw = raw.get("top_p")
        top_k_raw = raw.get("top_k")
        max_tokens_raw = raw.get("max_tokens")
        return ModeProfile(
            name=str(raw.get("name", name)),
            system_prompt_preamble=str(raw.get("system_prompt_preamble", "")),
            tool_subset=tuple(str(t) for t in tool_subset_raw),
            temperature=float(raw.get("temperature", 0.2)),
            top_p=None if top_p_raw is None else float(top_p_raw),
            top_k=None if top_k_raw is None else int(top_k_raw),
            max_tokens=None if max_tokens_raw is None else int(max_tokens_raw),
            max_turns=int(raw.get("max_turns", 40)),
            expected_runtime_minutes=int(raw.get("expected_runtime_minutes", 15)),
        )
    except (TypeError, ValueError) as exc:
        logger.warning("Invalid mode profile %r: %s", name, exc)
        return None


def load_profiles_from_dir(path: Path) -> dict[str, ModeProfile]:
    """Load mode profiles from ``<path>/*.yaml``; missing dir returns ``{}``.

    Each YAML file's stem becomes the profile name unless overridden by a
    ``name:`` key inside the document. Malformed files are skipped with a
    warning rather than crashing startup.
    """
    if not path.is_dir():
        return {}
    try:
        import yaml
    except ImportError:
        logger.warning("PyYAML not installed; skipping mode profile load from %s", path)
        return {}

    loaded: dict[str, ModeProfile] = {}
    for yaml_file in sorted(path.glob("*.yaml")):
        try:
            raw = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
        except (yaml.YAMLError, OSError) as exc:
            logger.warning("Failed to read mode profile %s: %s", yaml_file, exc)
            continue
        if not isinstance(raw, dict):
            logger.warning("Mode profile %s does not contain a mapping; skipped", yaml_file)
            continue
        profile = _coerce_profile(yaml_file.stem, cast(dict[str, Any], raw))
        if profile is not None:
            loaded[profile.name] = profile
    return loaded


def install_loaded_profiles(loaded: dict[str, ModeProfile]) -> None:
    """Merge *loaded* profiles into the module-level :data:`MODE_REGISTRY`.

    YAML-defined profiles override the in-code defaults of the same name.
    Unknown names are added so callers can register new modes from disk.
    """
    for name, profile in loaded.items():
        MODE_REGISTRY[name] = profile


@dataclass(frozen=True)
class AppliedMode:
    """Result of applying a profile to a spawn-time prompt and tool list.

    Attributes:
        profile: The mode profile that was applied.
        prompt: System prompt with the profile preamble prepended.
        tools: Tool list filtered through the profile's allowlist.
    """

    profile: ModeProfile
    prompt: str
    tools: list[str] = field(default_factory=list[str])


def apply_mode(
    profile: ModeProfile,
    *,
    prompt: str,
    tools: list[str] | None = None,
) -> AppliedMode:
    """Return an :class:`AppliedMode` carrying the prompt+tools after profile."""
    available = list(tools or [])
    return AppliedMode(
        profile=profile,
        prompt=profile.apply_preamble(prompt),
        tools=profile.filter_tools(available),
    )


def replace_profile(name: str, **changes: Any) -> ModeProfile:
    """Replace fields on the registry entry *name* and return the new instance."""
    current = MODE_REGISTRY[name]
    updated = _typed_replace(current, **changes)
    MODE_REGISTRY[name] = updated
    return updated


__all__ = [
    "MODE_REGISTRY",
    "AppliedMode",
    "ModeProfile",
    "apply_mode",
    "install_loaded_profiles",
    "load_profiles_from_dir",
    "replace_profile",
    "select_mode",
]
