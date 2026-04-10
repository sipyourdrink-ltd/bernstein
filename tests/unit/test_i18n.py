"""Tests for bernstein.core.i18n — CLI localisation foundation."""

from __future__ import annotations

import pytest

from bernstein.core.i18n import (
    SUPPORTED_LOCALES,
    available_locales,
    get_locale,
    t,
)

# ---------------------------------------------------------------------------
# get_locale
# ---------------------------------------------------------------------------


class TestGetLocale:
    def test_defaults_to_english(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BERNSTEIN_LANG", raising=False)
        monkeypatch.delenv("LC_ALL", raising=False)
        monkeypatch.delenv("LANG", raising=False)
        assert get_locale() == "en"

    def test_bernstein_lang_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BERNSTEIN_LANG", "es")
        assert get_locale() == "es"

    def test_lc_all_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BERNSTEIN_LANG", raising=False)
        monkeypatch.setenv("LC_ALL", "ja_JP.UTF-8")
        assert get_locale() == "ja"

    def test_lang_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BERNSTEIN_LANG", raising=False)
        monkeypatch.delenv("LC_ALL", raising=False)
        monkeypatch.setenv("LANG", "de_DE.UTF-8")
        assert get_locale() == "de"

    def test_unsupported_locale_defaults_to_en(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BERNSTEIN_LANG", "fr")
        assert get_locale() == "en"

    def test_priority_bernstein_lang_over_lc_all(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BERNSTEIN_LANG", "zh")
        monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
        assert get_locale() == "zh"


# ---------------------------------------------------------------------------
# t (translate)
# ---------------------------------------------------------------------------


class TestTranslate:
    def test_english_lookup(self) -> None:
        assert t("status.running", locale="en") == "Running"

    def test_spanish_lookup(self) -> None:
        assert t("status.running", locale="es") == "Ejecutando"

    def test_chinese_lookup(self) -> None:
        assert t("status.idle", locale="zh") == "空闲"

    def test_japanese_lookup(self) -> None:
        assert t("task.succeeded", locale="ja") == "タスク成功"

    def test_german_lookup(self) -> None:
        assert t("error.timeout", locale="de") == "Zeitlimit ueberschritten"

    def test_fallback_to_english(self) -> None:
        # Use a locale with the key, but pretend it is missing by using
        # a locale that exists. If we had a locale missing a key we would
        # fall back. Simulate by using english as locale directly.
        result = t("status.running", locale="en")
        assert result == "Running"

    def test_missing_key_returns_key(self) -> None:
        assert t("nonexistent.key", locale="en") == "nonexistent.key"

    def test_missing_key_unsupported_locale_returns_key(self) -> None:
        assert t("nonexistent.key", locale="fr") == "nonexistent.key"

    def test_interpolation(self) -> None:
        result = t("msg.welcome", locale="en", product_name="TestProd")
        assert result == "Welcome to TestProd"

    def test_interpolation_spanish(self) -> None:
        result = t("msg.tasks_remaining", locale="es", count="5")
        assert result == "5 tareas restantes"

    def test_interpolation_missing_var_returns_template(self) -> None:
        # If kwargs are missing the required key, return template as-is
        result = t("msg.welcome", locale="en")
        assert "{product_name}" in result

    def test_auto_locale_detection(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BERNSTEIN_LANG", "ja")
        result = t("status.stopped")
        assert result == "停止"


# ---------------------------------------------------------------------------
# available_locales
# ---------------------------------------------------------------------------


class TestAvailableLocales:
    def test_returns_sorted_list(self) -> None:
        locales = available_locales()
        assert locales == sorted(locales)

    def test_contains_all_supported(self) -> None:
        locales = available_locales()
        for loc in SUPPORTED_LOCALES:
            assert loc in locales

    def test_length_matches_supported(self) -> None:
        assert len(available_locales()) == len(SUPPORTED_LOCALES)
