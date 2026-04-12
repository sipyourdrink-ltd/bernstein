"""Organizational policy templates for Bernstein.

Allows org admins to define policy templates (YAML files) that are
auto-applied to Bernstein project configurations.  Each template
contains a set of config overrides that are merged (not replaced)
into the active SeedConfig dict.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import yaml

logger = logging.getLogger(__name__)


@dataclass
class OrgPolicyTemplate:
    """A single organizational policy template.

    Attributes:
        name: Human-readable policy name.
        description: What this policy enforces.
        overrides: Config fields to merge into the project SeedConfig.
    """

    name: str
    description: str
    overrides: dict[str, Any] = field(default_factory=lambda: dict[str, Any]())


def load_org_policies(paths: list[str]) -> list[OrgPolicyTemplate]:
    """Load organizational policy templates from YAML files.

    Each YAML file should contain a top-level mapping with ``name``,
    ``description``, and ``overrides`` keys.

    Args:
        paths: File paths to YAML policy template files.

    Returns:
        List of parsed policy templates.  Missing or malformed files
        are logged as warnings and skipped.
    """
    templates: list[OrgPolicyTemplate] = []
    for raw_path in paths:
        p = Path(raw_path)
        if not p.exists():
            logger.warning("Org policy file not found, skipping: %s", p)
            continue
        try:
            raw = yaml.safe_load(p.read_text())
            if not isinstance(raw, dict):
                logger.warning("Org policy file is not a YAML mapping: %s", p)
                continue
            data = cast("dict[str, Any]", raw)
            overrides = cast("dict[str, Any]", data.get("overrides", {}))
            templates.append(
                OrgPolicyTemplate(
                    name=str(data.get("name", p.stem)),
                    description=str(data.get("description", "")),
                    overrides=overrides,
                )
            )
        except (yaml.YAMLError, OSError) as exc:
            logger.warning("Failed to load org policy %s: %s", p, exc)
    return templates


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge *override* into *base*, returning a new dict.

    Lists and scalars in *override* replace those in *base*.
    Nested dicts are merged recursively.
    """
    merged: dict[str, Any] = dict(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(
                cast("dict[str, Any]", merged[key]),
                cast("dict[str, Any]", value),
            )
        else:
            merged[key] = value
    return merged


def apply_org_policies(
    config: dict[str, Any],
    templates: list[OrgPolicyTemplate],
) -> dict[str, Any]:
    """Apply organizational policy overrides to a config dict.

    Templates are applied in order.  Later templates override earlier
    ones if they set the same keys.  Dict values are deep-merged; all
    other types are replaced.

    Args:
        config: Base configuration dictionary.
        templates: Ordered list of policy templates to apply.

    Returns:
        New config dict with all policy overrides merged in.
    """
    result = dict(config)
    for tpl in templates:
        result = _deep_merge(result, tpl.overrides)
    return result
