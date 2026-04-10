"""Tests for bernstein.core.white_label — white-label branding support."""

from __future__ import annotations

from typing import TYPE_CHECKING

from bernstein.core.white_label import (
    WhiteLabelConfig,
    apply_branding,
    load_white_label,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# WhiteLabelConfig defaults
# ---------------------------------------------------------------------------


class TestWhiteLabelConfigDefaults:
    def test_default_product_name(self) -> None:
        cfg = WhiteLabelConfig()
        assert cfg.product_name == "Bernstein"

    def test_default_accent_color(self) -> None:
        cfg = WhiteLabelConfig()
        assert cfg.accent_color == "#6a1b9a"

    def test_default_vendor_empty(self) -> None:
        cfg = WhiteLabelConfig()
        assert cfg.vendor == ""

    def test_default_logo_path_empty(self) -> None:
        cfg = WhiteLabelConfig()
        assert cfg.logo_path == ""

    def test_default_support_url_empty(self) -> None:
        cfg = WhiteLabelConfig()
        assert cfg.support_url == ""


# ---------------------------------------------------------------------------
# load_white_label
# ---------------------------------------------------------------------------


class TestLoadWhiteLabel:
    def test_missing_file_returns_defaults(self, tmp_path: Path) -> None:
        cfg = load_white_label(tmp_path)
        assert cfg.product_name == "Bernstein"
        assert cfg.vendor == ""

    def test_loads_custom_branding(self, tmp_path: Path) -> None:
        branding = tmp_path / "branding.yaml"
        branding.write_text(
            "product_name: Acme Orchestrator\n"
            "vendor: Acme Corp\n"
            "accent_color: '#ff0000'\n"
            "support_url: https://acme.example.com/support\n"
        )
        cfg = load_white_label(tmp_path)
        assert cfg.product_name == "Acme Orchestrator"
        assert cfg.vendor == "Acme Corp"
        assert cfg.accent_color == "#ff0000"
        assert cfg.support_url == "https://acme.example.com/support"

    def test_partial_override(self, tmp_path: Path) -> None:
        branding = tmp_path / "branding.yaml"
        branding.write_text("vendor: MyCo\n")
        cfg = load_white_label(tmp_path)
        assert cfg.product_name == "Bernstein"  # default kept
        assert cfg.vendor == "MyCo"

    def test_invalid_yaml_returns_defaults(self, tmp_path: Path) -> None:
        branding = tmp_path / "branding.yaml"
        branding.write_text(":::not valid yaml:::\n\t\t[[[")
        cfg = load_white_label(tmp_path)
        assert cfg.product_name == "Bernstein"

    def test_non_mapping_yaml_returns_defaults(self, tmp_path: Path) -> None:
        branding = tmp_path / "branding.yaml"
        branding.write_text("- just\n- a\n- list\n")
        cfg = load_white_label(tmp_path)
        assert cfg.product_name == "Bernstein"


# ---------------------------------------------------------------------------
# apply_branding
# ---------------------------------------------------------------------------


class TestApplyBranding:
    def test_default_branding_variables(self) -> None:
        cfg = WhiteLabelConfig()
        variables = apply_branding(cfg)
        assert variables["product_name"] == "Bernstein"
        assert variables["title"] == "Bernstein"
        assert variables["footer"] == "Powered by Bernstein"

    def test_vendor_in_title(self) -> None:
        cfg = WhiteLabelConfig(product_name="Acme Agent", vendor="Acme Corp")
        variables = apply_branding(cfg)
        assert variables["title"] == "Acme Agent by Acme Corp"
        assert variables["footer"] == "Powered by Acme Corp"

    def test_all_config_fields_present(self) -> None:
        cfg = WhiteLabelConfig(
            product_name="X",
            vendor="Y",
            logo_path="/img/logo.png",
            accent_color="#123456",
            support_url="https://help.example.com",
        )
        variables = apply_branding(cfg)
        assert variables["product_name"] == "X"
        assert variables["vendor"] == "Y"
        assert variables["logo_path"] == "/img/logo.png"
        assert variables["accent_color"] == "#123456"
        assert variables["support_url"] == "https://help.example.com"
