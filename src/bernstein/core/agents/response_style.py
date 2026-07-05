"""Per-spawn response-style profiles (issue #2243).

Operators running many parallel workers need per-role control over worker
output verbosity. This module resolves a declared response style for every
spawn and renders the system-prompt addendum that carries it, so a reviewer
role can run terse while an investigator role runs verbose - without
hand-editing role templates.

Design constraints (load-bearing):

- **Deterministic resolution.** ``Task.metadata['mode']`` > role policy
  ``response_style`` > seed default (the ``role_model_policy.default``
  entry) > built-in ``"balanced"``. Same inputs always resolve to the same
  style and the same rendered addendum (snapshot-testable).
- **No new prompt dialect.** The addendum body is the mode-profile
  preamble from ``templates/mode_profiles/{fast,smart,deep}.yaml``; the
  style vocabulary (``verbose``/``balanced``/``terse``) is the conciseness
  vocabulary already defined by
  :class:`bernstein.core.agents.claude_model_prompts.PromptStrategy`.
- **Backward compatibility.** ``balanced`` is the neutral house style and
  renders an EMPTY addendum, so a spawn with no explicit profile is
  byte-identical to a pre-change spawn (the spawner previously hardcoded
  ``system_addendum=""``).
- **Typed failure.** A style whose mapped template file is missing from the
  resolved profile directory raises :class:`ResponseStyleTemplateError`
  instead of silently falling back to in-code defaults.

The rendered addendum's SHA-256 (:func:`addendum_sha256`) is recorded in
the task's cost ledger entry and audit trail, and is folded into the task
identity of the deterministic schedule projection when a profile is
explicitly declared (see ``orchestration/schedule_projection.py``).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from bernstein import _BUNDLED_TEMPLATES_DIR  # type: ignore[reportPrivateUsage]

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from pathlib import Path

#: Style vocabulary. Mirrors ``PromptStrategy.conciseness`` in
#: ``claude_model_prompts.py`` - tested for alignment, not re-derived, so
#: the two modules cannot silently diverge.
ResponseStyle = Literal["verbose", "balanced", "terse"]

RESPONSE_STYLES: tuple[str, ...] = ("verbose", "balanced", "terse")

#: The neutral default. Renders an empty addendum so unset-profile spawns
#: stay byte-identical to pre-change spawns.
DEFAULT_RESPONSE_STYLE: str = "balanced"

#: Style -> mode-profile template stem under ``templates/mode_profiles/``.
STYLE_TO_MODE_PROFILE: dict[str, str] = {
    "terse": "fast",
    "balanced": "smart",
    "verbose": "deep",
}

#: Mode-profile name -> style, so ``Task.metadata['mode']`` values that
#: carry the existing fast/smart/deep vocabulary resolve deterministically.
MODE_PROFILE_TO_STYLE: dict[str, str] = {v: k for k, v in STYLE_TO_MODE_PROFILE.items()}

#: Carve-outs baked into every non-empty addendum: content categories that
#: are never style-compressed regardless of the declared style.
_CARVE_OUTS = (
    "### Style carve-outs (verbatim zones)\n"
    "The response style applies to prose only. Never compress, truncate, or\n"
    "restyle code blocks, commit messages, error text, or completion API JSON\n"
    "payloads; reproduce them in full."
)


class ResponseStyleTemplateError(ValueError):
    """A declared response style references a missing or malformed template.

    Raised when the mode-profile template file mapped to a style cannot be
    read from the resolved profile directory. Config validation (the seed
    parser) surfaces this at parse time; the spawner surfaces it as a spawn
    failure if the file disappears between validation and spawn.
    """


@dataclass(frozen=True)
class ResolvedStyle:
    """Outcome of the deterministic style resolution.

    Attributes:
        style: One of :data:`RESPONSE_STYLES`.
        source: Which input supplied the style: ``"task_metadata"``,
            ``"role_policy"``, ``"seed_default"``, or ``"builtin_default"``.
        explicit: True when the style came from an operator-declared input
            (anything other than the built-in default). Only explicit
            profiles are folded into deterministic task identity.
    """

    style: str
    source: str
    explicit: bool


def _style_from_metadata_mode(value: object) -> str | None:
    """Map a ``Task.metadata['mode']`` value onto a style name, or ``None``."""
    if not isinstance(value, str):
        return None
    if value in RESPONSE_STYLES:
        return value
    return MODE_PROFILE_TO_STYLE.get(value)


def resolve_response_style(
    *,
    task_metadata: Mapping[str, Any],
    role_policy: Mapping[str, Any],
    default_policy: Mapping[str, Any],
) -> ResolvedStyle:
    """Resolve the response style for a spawn.

    Resolution order (deterministic, documented):

    1. ``task_metadata['mode']`` - accepts style names directly
       (``verbose``/``balanced``/``terse``) or the existing mode-profile
       names (``fast``/``smart``/``deep``) via :data:`MODE_PROFILE_TO_STYLE`.
    2. ``role_policy['response_style']`` - the per-role config entry.
    3. ``default_policy['response_style']`` - the seed-level default (the
       ``role_model_policy.default`` entry).
    4. :data:`DEFAULT_RESPONSE_STYLE` (``"balanced"``).

    Unknown values fall through to the next source rather than raising, so
    a stale metadata value cannot block a spawn; config-level typos are
    rejected earlier by the seed parser.

    Args:
        task_metadata: The task's metadata mapping (may be empty).
        role_policy: The resolved role policy entry for the task's role.
        default_policy: The ``role_model_policy.default`` entry (or empty).

    Returns:
        A :class:`ResolvedStyle` - never ``None``.
    """
    from_task = _style_from_metadata_mode(task_metadata.get("mode"))
    if from_task is not None:
        return ResolvedStyle(style=from_task, source="task_metadata", explicit=True)

    role_style = role_policy.get("response_style")
    if isinstance(role_style, str) and role_style in RESPONSE_STYLES:
        return ResolvedStyle(style=role_style, source="role_policy", explicit=True)

    seed_style = default_policy.get("response_style")
    if isinstance(seed_style, str) and seed_style in RESPONSE_STYLES:
        return ResolvedStyle(style=seed_style, source="seed_default", explicit=True)

    return ResolvedStyle(style=DEFAULT_RESPONSE_STYLE, source="builtin_default", explicit=False)


def _profiles_dir(workdir: Path | None) -> Path:
    """Return the profile directory: workdir override first, else bundled.

    Mirrors ``spawner_prompt._profiles_dir`` without importing the spawner
    stack (this module must stay light enough for the config layer).
    """
    if workdir is not None:
        candidate = workdir / "templates" / "mode_profiles"
        if candidate.is_dir():
            return candidate
    return _BUNDLED_TEMPLATES_DIR / "mode_profiles"


def style_template_path(style: str, workdir: Path | None = None) -> Path:
    """Return the template file path a style renders from.

    Raises:
        ValueError: If *style* is not in :data:`RESPONSE_STYLES`.
    """
    profile_name = STYLE_TO_MODE_PROFILE.get(style)
    if profile_name is None:
        raise ValueError(f"unknown response style {style!r}; expected one of {RESPONSE_STYLES}")
    return _profiles_dir(workdir) / f"{profile_name}.yaml"


def _load_preamble(style: str, workdir: Path | None) -> str:
    """Read the mode-profile preamble the style maps to.

    Reads the YAML file directly (no global registry mutation) so the
    rendered addendum is a pure function of the template bytes on disk.

    Raises:
        ResponseStyleTemplateError: If the file is missing or malformed.
    """
    path = style_template_path(style, workdir)
    if not path.is_file():
        raise ResponseStyleTemplateError(
            f"response style {style!r} maps to template {path.name!r} which is missing from {path.parent}"
        )
    import yaml

    try:
        raw: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError) as exc:
        raise ResponseStyleTemplateError(f"cannot read response-style template {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ResponseStyleTemplateError(f"response-style template {path} must contain a YAML mapping")
    preamble = raw.get("system_prompt_preamble")
    if not isinstance(preamble, str) or not preamble.strip():
        raise ResponseStyleTemplateError(f"response-style template {path} has no 'system_prompt_preamble'")
    return preamble


def render_style_addendum(style: str, *, workdir: Path | None = None) -> str:
    """Render the system-prompt addendum for *style*.

    ``balanced`` renders an empty string: it declares the neutral house
    style, and an empty addendum keeps unset-profile spawns byte-identical
    to pre-change spawns (the spawner previously hardcoded
    ``system_addendum=""``).

    ``terse``/``verbose`` render the mapped mode-profile preamble plus the
    fixed carve-out block. Given identical template files the output is
    byte-identical across machines (snapshot-tested).

    Args:
        style: One of :data:`RESPONSE_STYLES`.
        workdir: Project root used to locate ``templates/mode_profiles/``;
            ``None`` uses the bundled templates.

    Returns:
        The rendered addendum ("" for ``balanced``).

    Raises:
        ValueError: If *style* is unknown.
        ResponseStyleTemplateError: If the mapped template is missing or
            malformed (AC4 - typed error, no silent fallback).
    """
    if style not in RESPONSE_STYLES:
        raise ValueError(f"unknown response style {style!r}; expected one of {RESPONSE_STYLES}")
    if style == DEFAULT_RESPONSE_STYLE:
        return ""
    preamble = _load_preamble(style, workdir)
    return f"## Response style: {style}\n\n{preamble.rstrip()}\n\n{_CARVE_OUTS}"


def addendum_sha256(addendum: str) -> str:
    """SHA-256 hex digest of the rendered addendum's UTF-8 bytes."""
    return hashlib.sha256(addendum.encode("utf-8")).hexdigest()


def validate_style_templates(styles: Iterable[str], *, workdir: Path | None = None) -> None:
    """Validate that every style in *styles* can be rendered.

    Config-validation entry point (AC4): called by the seed parser so a
    role policy that declares a style whose template file is missing fails
    at parse time with a typed error rather than at spawn time.

    Raises:
        ValueError: If a style name is unknown.
        ResponseStyleTemplateError: If a mapped template file is missing or
            malformed.
    """
    for style in sorted(set(styles)):
        render_style_addendum(style, workdir=workdir)


__all__ = [
    "DEFAULT_RESPONSE_STYLE",
    "MODE_PROFILE_TO_STYLE",
    "RESPONSE_STYLES",
    "STYLE_TO_MODE_PROFILE",
    "ResolvedStyle",
    "ResponseStyle",
    "ResponseStyleTemplateError",
    "addendum_sha256",
    "render_style_addendum",
    "resolve_response_style",
    "style_template_path",
    "validate_style_templates",
]
