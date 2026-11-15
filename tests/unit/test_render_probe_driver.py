"""Optional render-probe driver contract, with an in-process browser fake."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from bernstein.core.evidence.render_receipt import EnvironmentDescriptor, Viewport
from bernstein.core.orchestration.browser_driver import PlaywrightBrowserDriver, RenderProbeDriver


def _environment() -> EnvironmentDescriptor:
    return EnvironmentDescriptor(
        engine_build_identity="chromium-120.0",
        viewport=Viewport(800, 600),
        device_pixel_ratio=1.0,
        locale="en-US",
        timezone="UTC",
        clock_value=datetime(2026, 11, 15, tzinfo=UTC),
        font_set_hash="sha256:" + "a" * 64,
        animation_disabled=True,
        caret_disabled=True,
        reduced_motion=True,
        colour_scheme="light",
    )


class FakePage:
    url = "file:///sample.html"

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        env = _environment().to_dict()
        self.result = {
            "environment": {
                key: value
                for key, value in env.items()
                if key not in ("engine_build_identity", "font_set_hash", "animation_disabled", "caret_disabled")
            },
            "computed_styles": [
                {
                    "element_path": "sample",
                    "properties": {
                        "display": "block",
                        "color": "rgb(255, 0, 0)",
                        "width": "120px",
                        "height": "40px",
                        "font-family": "sans-serif",
                        "font-size": "16px",
                        "visibility": "visible",
                        "opacity": "1",
                    },
                }
            ],
        }

    def evaluate(self, script: str, arguments: object) -> object:
        self.calls.append((script, arguments))
        assert "getComputedStyle" in script
        assert "querySelectorAll" in script
        return self.result


def _driver(tmp_path: Path) -> tuple[PlaywrightBrowserDriver, FakePage]:
    page = FakePage()
    context = SimpleNamespace(browser=SimpleNamespace(version="120.0", browser_type=SimpleNamespace(name="chromium")))
    driver = PlaywrightBrowserDriver(context, page, profile_dir=tmp_path, build_id="chromium-120.0")
    return driver, page


def test_pinned_driver_captures_bounded_computed_properties(tmp_path: Path) -> None:
    driver, page = _driver(tmp_path)
    assert isinstance(driver, RenderProbeDriver)
    result = driver.render_probe(environment=_environment(), elements={"sample": "#sample"})
    assert result.available
    assert result.environment == _environment()
    assert result.computed_styles[0].properties["color"] == "rgb(255, 0, 0)"
    assert result.computed_styles[0].properties["width"] == "120px"
    assert result.property_vocabulary_version == "computed-css-v1"
    assert len(page.calls) == 1
    assert page.calls[0][1] == {"elements": {"sample": "#sample"}}


@pytest.mark.parametrize(
    "change",
    [
        {"engine_build_identity": ""},
        {"font_set_hash": ""},
        {"locale": ""},
        {"timezone": ""},
        {"viewport": Viewport()},
        {"device_pixel_ratio": float("nan")},
        {"clock_value": datetime(2026, 11, 15)},
    ],
)
def test_incomplete_environment_is_unavailable_without_probe(tmp_path: Path, change: dict[str, object]) -> None:
    driver, page = _driver(tmp_path)
    environment = replace(_environment(), **change)
    result = driver.render_probe(environment=environment, elements={"sample": "#sample"})
    assert not result.available
    assert result.reason
    assert result.environment is None
    assert result.computed_styles == ()
    assert page.calls == []


def test_unpinned_or_different_actual_engine_is_unavailable(tmp_path: Path) -> None:
    driver, page = _driver(tmp_path)
    driver.build_id = "unversioned"
    assert not driver.render_probe(environment=_environment(), elements={"sample": "#sample"}).available
    driver.build_id = "chromium-120.0"
    driver.context.browser.version = "121.0"
    assert not driver.render_probe(environment=_environment(), elements={"sample": "#sample"}).available
    assert page.calls == []


def test_observed_environment_mismatch_is_unavailable(tmp_path: Path) -> None:
    driver, page = _driver(tmp_path)
    page.result["environment"]["locale"] = "other"
    result = driver.render_probe(environment=_environment(), elements={"sample": "#sample"})
    assert not result.available
    assert "locale" in result.reason
    assert result.environment is None
    assert result.computed_styles == ()


def test_missing_element_never_becomes_an_empty_successful_capture(tmp_path: Path) -> None:
    driver, page = _driver(tmp_path)
    page.result = {"unavailable": "selector must match exactly one element: sample"}
    result = driver.render_probe(environment=_environment(), elements={"sample": "#missing"})
    assert not result.available
    assert "sample" in result.reason


def test_bad_payload_never_yields_partial_properties(tmp_path: Path) -> None:
    driver, page = _driver(tmp_path)
    page.result["computed_styles"] = []
    result = driver.render_probe(environment=_environment(), elements={"sample": "#sample"})
    assert not result.available
    assert result.computed_styles == ()


@pytest.mark.parametrize("value", [None, {}, "", "   "])
def test_invalid_css_values_are_unavailable(tmp_path: Path, value: object) -> None:
    driver, page = _driver(tmp_path)
    page.result["computed_styles"][0]["properties"]["width"] = value
    result = driver.render_probe(environment=_environment(), elements={"sample": "#sample"})
    assert not result.available
    assert result.computed_styles == ()


@pytest.mark.parametrize("value", ["", None, "1970-01-01T00:00:00"])
def test_invalid_observed_clock_cannot_match_epoch_default(tmp_path: Path, value: object) -> None:
    driver, page = _driver(tmp_path)
    page.result["environment"]["clock_value"] = value
    result = driver.render_probe(
        environment=replace(_environment(), clock_value=datetime(1970, 1, 1, tzinfo=UTC)),
        elements={"sample": "#sample"},
    )
    assert not result.available


@pytest.mark.parametrize("flag", ["animation_disabled", "caret_disabled", "reduced_motion"])
@pytest.mark.parametrize("value", [None, "false", 1])
def test_control_flags_require_boolean_declarations(tmp_path: Path, flag: str, value: object) -> None:
    driver, page = _driver(tmp_path)
    result = driver.render_probe(environment=replace(_environment(), **{flag: value}), elements={"sample": "#sample"})
    assert not result.available
    assert page.calls == []


def test_false_control_declarations_are_valid(tmp_path: Path) -> None:
    driver, _ = _driver(tmp_path)
    result = driver.render_probe(
        environment=replace(_environment(), animation_disabled=False, caret_disabled=False),
        elements={"sample": "#sample"},
    )
    assert result.available


def test_observed_boolean_is_not_an_integer(tmp_path: Path) -> None:
    driver, page = _driver(tmp_path)
    page.result["environment"]["reduced_motion"] = 1
    assert not driver.render_probe(environment=_environment(), elements={"sample": "#sample"}).available
