"""White-label branding support.

Allows Bernstein to be rebranded for OEM / enterprise customers.
Reads optional ``branding.yaml`` from the config directory and exposes
template variables that the CLI and dashboard can use for display.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

_BRANDING_FILENAME = "branding.yaml"


@dataclass
class WhiteLabelConfig:
    """Branding configuration for a white-labelled deployment.

    Attributes:
        product_name: Display name shown in CLI banners and dashboards.
        vendor: Organisation or company name.
        logo_path: Filesystem path to a logo image (may be empty).
        accent_color: Hex colour code for UI accents.
        support_url: URL to vendor support / docs site.
    """

    product_name: str = "Bernstein"
    vendor: str = ""
    logo_path: str = ""
    accent_color: str = "#6a1b9a"
    support_url: str = ""


def load_white_label(config_dir: Path) -> WhiteLabelConfig:
    """Load white-label configuration from *config_dir*/branding.yaml.

    Falls back to defaults when the file is missing or unparseable.

    Args:
        config_dir: Directory that may contain ``branding.yaml``.

    Returns:
        Populated ``WhiteLabelConfig``.
    """
    branding_path = config_dir / _BRANDING_FILENAME
    if not branding_path.exists():
        return WhiteLabelConfig()

    try:
        import yaml

        raw: Any = yaml.safe_load(branding_path.read_text())
        if not isinstance(raw, dict):
            logger.warning("branding.yaml is not a mapping; using defaults")
            return WhiteLabelConfig()

        data = cast("dict[str, Any]", raw)
        return WhiteLabelConfig(
            product_name=str(data.get("product_name", "Bernstein")),
            vendor=str(data.get("vendor", "")),
            logo_path=str(data.get("logo_path", "")),
            accent_color=str(data.get("accent_color", "#6a1b9a")),
            support_url=str(data.get("support_url", "")),
        )
    except Exception as exc:
        logger.warning("Failed to load branding.yaml: %s; using defaults", exc)
        return WhiteLabelConfig()


def apply_branding(config: WhiteLabelConfig) -> dict[str, str]:
    """Return template variables derived from the branding config.

    These are suitable for string interpolation in CLI help text,
    dashboard templates, and notification messages.

    Args:
        config: The active ``WhiteLabelConfig``.

    Returns:
        Dict of template variable names to string values.
    """
    variables: dict[str, str] = {
        "product_name": config.product_name,
        "vendor": config.vendor,
        "logo_path": config.logo_path,
        "accent_color": config.accent_color,
        "support_url": config.support_url,
        "title": (f"{config.product_name} by {config.vendor}" if config.vendor else config.product_name),
        "footer": (f"Powered by {config.vendor}" if config.vendor else f"Powered by {config.product_name}"),
    }
    return variables
