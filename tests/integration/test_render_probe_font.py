"""Local browser regression for resolved versus failed font resources."""

from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest

from bernstein.core.evidence.render_receipt import EnvironmentDescriptor, Viewport
from bernstein.core.orchestration.browser_driver import PlaywrightBrowserDriver


@pytest.mark.parametrize("font_succeeds", [True, False])
def test_failed_font_cannot_produce_comparable_render_evidence(tmp_path: Path, font_succeeds: bool) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    font = (Path(__file__).parents[2] / "web/src/fonts/inter-tight-v9-latin.woff2").read_bytes()
    with playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(headless=True)
        except playwright.Error as exc:
            if "Executable doesn't exist" in str(exc):
                pytest.skip("Playwright Chromium is not installed")
            raise
        try:
            context = browser.new_context(
                viewport={"width": 800, "height": 600},
                locale="en-US",
                timezone_id="UTC",
                reduced_motion="reduce",
                color_scheme="light",
            )
            if font_succeeds:
                context.route(
                    "https://render-probe.invalid/font.woff2",
                    lambda route: route.fulfill(
                        body=font,
                        content_type="font/woff2",
                        headers={"Access-Control-Allow-Origin": "*"},
                    ),
                )
            else:
                context.route("https://render-probe.invalid/font.woff2", lambda route: route.abort("failed"))
            page = context.new_page()
            fixed = datetime(2026, 11, 15, tzinfo=UTC)
            page.clock.set_fixed_time(fixed)
            page.set_content("""<style>@font-face {font-family: Probe; src: url("https://render-probe.invalid/font.woff2")}
                #sample {font-family: Probe, monospace; display: inline-block}</style><div id="sample">iiiiii</div>""")
            page.evaluate("() => document.fonts.ready")
            build = "chromium-" + browser.version
            driver = PlaywrightBrowserDriver(context, page, profile_dir=tmp_path, build_id=build)
            environment = EnvironmentDescriptor(
                engine_build_identity=build,
                viewport=Viewport(800, 600),
                device_pixel_ratio=1,
                locale="en-US",
                timezone="UTC",
                clock_value=fixed,
                font_set_hash="sha256:" + sha256(font).hexdigest(),
                animation_disabled=False,
                caret_disabled=False,
                reduced_motion=True,
                colour_scheme="light",
            )
            result = driver.render_probe(environment=environment, elements={"sample": "#sample"})
            assert result.available is font_succeeds
            if not font_succeeds:
                assert "font" in result.reason
                assert result.environment is None
                assert result.computed_styles == ()
        finally:
            browser.close()
