"""Dashboard colour tokens must satisfy WCAG AA contrast thresholds (#3589).

The web dashboard uses CSS custom properties declared as space-separated HSL
triples in ``web/src/index.css`` for both ``:root`` (light theme) and ``.dark``
(dark theme). State primitives and UI components in ``web/src/lib/states.tsx``
and ``web/src/components/`` compose these tokens for body text, metadata,
action buttons, semantic badges, and pill indicators.

Under WCAG 2.1 Success Criterion 1.4.3 (Contrast Minimum), normal text requires
a contrast ratio of at least 4.5:1 against its background. Non-text UI
components (WCAG 1.4.11) and subtle structural borders are measured and
recorded.

These tests dynamically parse the stylesheet tokens, convert HSL values to
sRGB and relative luminance, compute contrast ratios for all paired foreground
and background surfaces, and gate them against WCAG AA requirements. A self-
check verifies that pre-fix lightness values (e.g. 51% on light background or
38% on dark card) fail the check, proving the detector guards against
contrast regressions.
"""

from __future__ import annotations

import colorsys
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INDEX_CSS = REPO_ROOT / "web" / "src" / "index.css"

WCAG_AA_TEXT_THRESHOLD = 4.5
WCAG_NON_TEXT_THRESHOLD = 3.0

#: Core text and interactive token pairs used across dashboard screens.
CORE_TEXT_PAIRS = [
    # Base text & surfaces
    ("foreground", "background"),
    ("foreground", "card"),
    ("card-foreground", "card"),
    ("popover-foreground", "popover"),
    # Emphasis / Action buttons
    ("primary-foreground", "primary"),
    ("secondary-foreground", "secondary"),
    ("accent-foreground", "accent"),
    ("destructive-foreground", "destructive"),
    ("success-foreground", "success"),
    ("warning-foreground", "warning"),
    # Recessive & metadata text
    ("muted-foreground", "background"),
    ("muted-foreground", "card"),
    ("muted-foreground", "surface-raised"),
    ("meta-foreground", "background"),
    ("meta-foreground", "card"),
    ("meta-foreground", "surface-raised"),
    # Semantic text on surfaces
    ("accent", "card"),
    ("accent", "background"),
    ("accent", "surface-raised"),
    ("destructive", "card"),
    ("destructive", "background"),
    ("destructive", "surface-raised"),
    ("success", "card"),
    ("success", "background"),
    ("success", "surface-raised"),
    ("warning", "card"),
    ("warning", "background"),
    ("warning", "surface-raised"),
]

#: Pill variant pairings defined in web/src/lib/states.tsx
PILL_PAIRS = [
    ("default", "muted-foreground", "surface-raised"),
    ("accent-strong", "accent-foreground", "accent"),
    ("accent", "accent", "card"),
    ("accent-surface", "accent", "surface-raised"),
    ("success", "success", "card"),
    ("success-surface", "success", "surface-raised"),
    ("warning", "warning", "card"),
    ("warning-surface", "warning", "surface-raised"),
    ("danger", "destructive", "card"),
    ("danger-surface", "destructive", "surface-raised"),
    ("ghost", "muted-foreground", "card"),
]


def parse_hsl_components(raw_value: str) -> tuple[float, float, float]:
    """Parse space-separated HSL value such as '40 37.5% 96.9%'."""
    clean = re.sub(r"/\*.*?\*/", "", raw_value).strip()
    parts = clean.split()
    if len(parts) < 3:
        msg = f"Invalid HSL string: {raw_value!r}"
        raise ValueError(msg)
    h = float(parts[0])
    s = float(parts[1].rstrip("%"))
    l = float(parts[2].rstrip("%"))
    return h, s, l


def hsl_to_srgb(h: float, s: float, l: float) -> tuple[float, float, float]:
    """Convert HSL (degrees, percent, percent) to sRGB in range [0, 1]."""
    return colorsys.hls_to_rgb(h / 360.0, l / 100.0, s / 100.0)


def relative_luminance(rgb: tuple[float, float, float]) -> float:
    """Calculate WCAG 2.1 relative luminance for linearised sRGB channels."""

    def channel_lum(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r_lin, g_lin, b_lin = (channel_lum(c) for c in rgb)
    return 0.2126 * r_lin + 0.7152 * g_lin + 0.0722 * b_lin


def contrast_ratio(rgb1: tuple[float, float, float], rgb2: tuple[float, float, float]) -> float:
    """Compute WCAG contrast ratio (L1 + 0.05) / (L2 + 0.05) between two sRGB colours."""
    lum1 = relative_luminance(rgb1)
    lum2 = relative_luminance(rgb2)
    lighter = max(lum1, lum2)
    darker = min(lum1, lum2)
    return (lighter + 0.05) / (darker + 0.05)


def parse_css_tokens(css_content: str) -> dict[str, dict[str, tuple[float, float, float]]]:
    """Extract HSL colour tokens from :root (light) and .dark blocks."""
    root_match = re.search(r":root\s*\{([^}]+)\}", css_content, re.DOTALL)
    dark_match = re.search(r"\.dark\s*\{([^}]+)\}", css_content, re.DOTALL)

    if not root_match or not dark_match:
        msg = "Could not locate :root or .dark blocks in stylesheet"
        raise ValueError(msg)

    def extract_vars(block: str) -> dict[str, tuple[float, float, float]]:
        tokens = {}
        for line in block.splitlines():
            line = line.strip()
            if line.startswith("--") and ":" in line:
                var_name, val = line.split(":", 1)
                var_name = var_name.strip().lstrip("-")
                val_clean = val.split(";")[0].strip()
                val_clean = re.sub(r"/\*.*?\*/", "", val_clean).strip()
                if "%" in val_clean:
                    try:
                        tokens[var_name] = parse_hsl_components(val_clean)
                    except ValueError:
                        continue
        return tokens

    return {
        "light": extract_vars(root_match.group(1)),
        "dark": extract_vars(dark_match.group(1)),
    }


def get_token_rgb(tokens: dict[str, tuple[float, float, float]], token_name: str) -> tuple[float, float, float]:
    """Resolve token name to sRGB colour."""
    clean_name = token_name.lstrip("-")
    if clean_name not in tokens:
        msg = f"Token --{clean_name} not found in theme"
        raise KeyError(msg)
    return hsl_to_srgb(*tokens[clean_name])


def test_stylesheet_defines_both_light_and_dark_themes() -> None:
    """The index.css stylesheet must declare both :root and .dark token sets."""
    assert INDEX_CSS.is_file(), f"Stylesheet missing at {INDEX_CSS}"
    css_content = INDEX_CSS.read_text(encoding="utf-8")
    tokens = parse_css_tokens(css_content)

    assert "light" in tokens and "dark" in tokens
    assert "foreground" in tokens["light"]
    assert "background" in tokens["light"]
    assert "foreground" in tokens["dark"]
    assert "background" in tokens["dark"]


@pytest.mark.parametrize("theme_name", ["light", "dark"])
@pytest.mark.parametrize(
    ("fg_token", "bg_token"),
    CORE_TEXT_PAIRS,
    ids=[f"{fg}-on-{bg}" for fg, bg in CORE_TEXT_PAIRS],
)
def test_all_core_text_pairs_meet_wcag_aa_threshold(theme_name: str, fg_token: str, bg_token: str) -> None:
    """Every text and button pairing in the dashboard must meet WCAG AA (≥ 4.5:1)."""
    tokens = parse_css_tokens(INDEX_CSS.read_text(encoding="utf-8"))[theme_name]

    fg_rgb = get_token_rgb(tokens, fg_token)
    bg_rgb = get_token_rgb(tokens, bg_token)
    ratio = contrast_ratio(fg_rgb, bg_rgb)

    assert ratio >= WCAG_AA_TEXT_THRESHOLD, (
        f"Theme '{theme_name}': token pair --{fg_token} on --{bg_token} "
        f"has contrast {ratio:.2f}:1, failing WCAG AA (required ≥ {WCAG_AA_TEXT_THRESHOLD}:1)."
    )


@pytest.mark.parametrize("theme_name", ["light", "dark"])
@pytest.mark.parametrize(
    ("pill_id", "fg_token", "bg_token"),
    PILL_PAIRS,
    ids=[pill[0] for pill in PILL_PAIRS],
)
def test_all_pill_kinds_meet_wcag_contrast(theme_name: str, pill_id: str, fg_token: str, bg_token: str) -> None:
    """Pill components from web/src/lib/states.tsx must satisfy contrast standards."""
    tokens = parse_css_tokens(INDEX_CSS.read_text(encoding="utf-8"))[theme_name]

    fg_rgb = get_token_rgb(tokens, fg_token)
    bg_rgb = get_token_rgb(tokens, bg_token)
    ratio = contrast_ratio(fg_rgb, bg_rgb)

    assert ratio >= WCAG_AA_TEXT_THRESHOLD, (
        f"Theme '{theme_name}': Pill '{pill_id}' (--{fg_token} on --{bg_token}) "
        f"has contrast {ratio:.2f}:1, failing WCAG AA (required ≥ {WCAG_AA_TEXT_THRESHOLD}:1)."
    )


def test_non_text_contrast_measurements_are_recorded() -> None:
    """Non-text tokens (borders, scrollbars) must have documented contrast ratios."""
    css_content = INDEX_CSS.read_text(encoding="utf-8")
    tokens = parse_css_tokens(css_content)

    for theme_name in ("light", "dark"):
        theme = tokens[theme_name]
        bg_rgb = get_token_rgb(theme, "background")
        border_strong_rgb = get_token_rgb(theme, "border-strong")
        border_rgb = get_token_rgb(theme, "border")
        border_subtle_rgb = get_token_rgb(theme, "border-subtle")

        ratio_strong = contrast_ratio(border_strong_rgb, bg_rgb)
        ratio_border = contrast_ratio(border_rgb, bg_rgb)
        ratio_subtle = contrast_ratio(border_subtle_rgb, bg_rgb)

        # Strong borders (used for scrollbars and active framing) should exceed 2:1
        assert ratio_strong >= 2.0, (
            f"Theme '{theme_name}': --border-strong on --background contrast {ratio_strong:.2f}:1 is too low"
        )
        # Subtle/default borders are low-contrast dividers
        assert ratio_border > 1.0
        assert ratio_subtle > 1.0


def test_the_contrast_check_catches_the_pre_fix_meta_foreground_contrast() -> None:
    """Prove the detector fires on failing ratios rather than passing vacuously.

    Before lightness was tuned:
    - Light theme meta-foreground was '40 9.6% 51%', yielding 3.40:1 on background (< 4.5:1).
    - Dark theme meta-foreground was '45 10.3% 38%', yielding 3.04:1 on card (< 4.5:1).
    """
    # Light theme pre-fix check
    light_bg_rgb = hsl_to_srgb(40, 37.5, 96.9)
    light_meta_prefix_rgb = hsl_to_srgb(40, 9.6, 51.0)
    light_prefix_ratio = contrast_ratio(light_meta_prefix_rgb, light_bg_rgb)
    assert light_prefix_ratio < WCAG_AA_TEXT_THRESHOLD, (
        f"Expected pre-fix light meta-foreground to fail WCAG AA, got {light_prefix_ratio:.2f}:1"
    )
    assert 3.39 <= light_prefix_ratio <= 3.42

    # Dark theme pre-fix check
    dark_card_rgb = hsl_to_srgb(60, 10.6, 9.2)
    dark_meta_prefix_rgb = hsl_to_srgb(45, 10.3, 38.0)
    dark_prefix_ratio = contrast_ratio(dark_meta_prefix_rgb, dark_card_rgb)
    assert dark_prefix_ratio < WCAG_AA_TEXT_THRESHOLD, (
        f"Expected pre-fix dark meta-foreground to fail WCAG AA, got {dark_prefix_ratio:.2f}:1"
    )
    assert 3.03 <= dark_prefix_ratio <= 3.06
