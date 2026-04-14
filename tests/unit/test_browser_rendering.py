"""Unit tests for BrowserRenderingBridge."""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from bernstein.bridges.browser_rendering import (
    BrowserConfig,
    BrowserRenderingBridge,
    BrowserRenderingError,
    PageResult,
    ScrapedData,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(
    *,
    account_id: str = "acct-abc",
    api_token: str = "cf-token-123",
    timeout_seconds: int = 30,
    viewport_width: int = 1280,
    viewport_height: int = 720,
) -> BrowserConfig:
    return BrowserConfig(
        account_id=account_id,
        api_token=api_token,
        timeout_seconds=timeout_seconds,
        viewport_width=viewport_width,
        viewport_height=viewport_height,
    )


def _mock_response(
    status_code: int = 200,
    json_data: dict | None = None,
    content: bytes = b"",
) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.content = content
    resp.text = content.decode("utf-8", errors="replace") if content else ""
    return resp


# ---------------------------------------------------------------------------
# BrowserConfig defaults and custom values
# ---------------------------------------------------------------------------


class TestBrowserConfig:
    def test_defaults(self) -> None:
        cfg = BrowserConfig(account_id="acct", api_token="tok")
        assert cfg.timeout_seconds == 30
        assert cfg.viewport_width == 1280
        assert cfg.viewport_height == 720
        assert cfg.user_agent == "BernsteinBot/1.0"
        assert cfg.block_ads is True
        assert cfg.javascript_enabled is True

    def test_custom_values(self) -> None:
        cfg = BrowserConfig(
            account_id="acct",
            api_token="tok",
            timeout_seconds=60,
            viewport_width=1920,
            viewport_height=1080,
            user_agent="CustomBot/2.0",
            block_ads=False,
            javascript_enabled=False,
        )
        assert cfg.timeout_seconds == 60
        assert cfg.viewport_width == 1920
        assert cfg.viewport_height == 1080
        assert cfg.user_agent == "CustomBot/2.0"
        assert cfg.block_ads is False
        assert cfg.javascript_enabled is False

    def test_frozen(self) -> None:
        cfg = BrowserConfig(account_id="acct", api_token="tok")
        with pytest.raises(AttributeError):
            cfg.account_id = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# PageResult and ScrapedData construction
# ---------------------------------------------------------------------------


class TestDataclasses:
    def test_page_result_defaults(self) -> None:
        result = PageResult(url="https://example.com", title="Test", content="Hello")
        assert result.url == "https://example.com"
        assert result.title == "Test"
        assert result.content == "Hello"
        assert result.html == ""
        assert result.screenshot_base64 == ""
        assert result.status_code == 200
        assert result.load_time_ms == 0.0
        assert result.links == []
        assert result.metadata == {}

    def test_page_result_with_all_fields(self) -> None:
        result = PageResult(
            url="https://example.com",
            title="Test",
            content="Hello",
            html="<h1>Hello</h1>",
            screenshot_base64="abc123",
            status_code=200,
            load_time_ms=42.5,
            links=["https://example.com/a"],
            metadata={"key": "value"},
        )
        assert result.html == "<h1>Hello</h1>"
        assert result.links == ["https://example.com/a"]

    def test_scraped_data(self) -> None:
        data = ScrapedData(
            url="https://example.com",
            selector=".article",
            elements=[{"text": "Hello", "href": "/link"}],
        )
        assert data.selector == ".article"
        assert len(data.elements) == 1

    def test_scraped_data_defaults(self) -> None:
        data = ScrapedData(url="https://example.com", selector="div")
        assert data.elements == []


# ---------------------------------------------------------------------------
# BrowserRenderingBridge __init__ validation
# ---------------------------------------------------------------------------


class TestBridgeInit:
    def test_valid_config(self) -> None:
        bridge = BrowserRenderingBridge(_make_config())
        assert bridge.config.account_id == "acct-abc"

    def test_missing_account_id_raises(self) -> None:
        with pytest.raises(BrowserRenderingError, match="non-empty account_id"):
            BrowserRenderingBridge(_make_config(account_id=""))

    def test_missing_api_token_raises(self) -> None:
        with pytest.raises(BrowserRenderingError, match="non-empty api_token"):
            BrowserRenderingBridge(_make_config(api_token=""))


# ---------------------------------------------------------------------------
# _build_headers
# ---------------------------------------------------------------------------


class TestBuildHeaders:
    def test_includes_auth_token(self) -> None:
        bridge = BrowserRenderingBridge(_make_config(api_token="my-secret-token"))
        headers = bridge._build_headers()
        assert headers["Authorization"] == "Bearer my-secret-token"
        assert headers["Content-Type"] == "application/json"


# ---------------------------------------------------------------------------
# _api_url
# ---------------------------------------------------------------------------


class TestApiUrl:
    def test_builds_correct_url(self) -> None:
        bridge = BrowserRenderingBridge(_make_config(account_id="acct-xyz"))
        url = bridge._api_url("render")
        assert url == ("https://api.cloudflare.com/client/v4/accounts/acct-xyz/browser-rendering/render")


# ---------------------------------------------------------------------------
# render()
# ---------------------------------------------------------------------------


class TestRender:
    @pytest.mark.asyncio
    async def test_render_success(self) -> None:
        bridge = BrowserRenderingBridge(_make_config())
        mock_resp = _mock_response(
            200,
            json_data={
                "title": "Example",
                "content": "Hello world",
                "statusCode": 200,
                "loadTimeMs": 150.0,
                "links": ["https://example.com/about"],
                "metadata": {"lang": "en"},
            },
        )
        bridge._client.post = AsyncMock(return_value=mock_resp)

        result = await bridge.render("https://example.com")
        assert result.title == "Example"
        assert result.content == "Hello world"
        assert result.status_code == 200
        assert result.links == ["https://example.com/about"]
        assert result.html == ""
        assert result.screenshot_base64 == ""
        bridge._client.post.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_render_with_screenshot(self) -> None:
        bridge = BrowserRenderingBridge(_make_config())
        mock_resp = _mock_response(
            200,
            json_data={
                "title": "Example",
                "content": "Hello",
                "screenshot": "iVBORw0KGgo=",
            },
        )
        bridge._client.post = AsyncMock(return_value=mock_resp)

        result = await bridge.render("https://example.com", screenshot=True)
        assert result.screenshot_base64 == "iVBORw0KGgo="

    @pytest.mark.asyncio
    async def test_render_with_full_html(self) -> None:
        bridge = BrowserRenderingBridge(_make_config())
        mock_resp = _mock_response(
            200,
            json_data={
                "title": "Example",
                "content": "Hello",
                "html": "<html><body>Hello</body></html>",
            },
        )
        bridge._client.post = AsyncMock(return_value=mock_resp)

        result = await bridge.render("https://example.com", full_html=True)
        assert result.html == "<html><body>Hello</body></html>"

    @pytest.mark.asyncio
    async def test_render_timeout(self) -> None:
        bridge = BrowserRenderingBridge(_make_config())
        bridge._client.post = AsyncMock(side_effect=httpx.ReadTimeout("read timed out"))

        with pytest.raises(BrowserRenderingError, match="Timeout"):
            await bridge.render("https://example.com")

    @pytest.mark.asyncio
    async def test_render_404(self) -> None:
        bridge = BrowserRenderingBridge(_make_config())
        mock_resp = _mock_response(404, content=b"not found")
        bridge._client.post = AsyncMock(return_value=mock_resp)

        with pytest.raises(BrowserRenderingError, match="404"):
            await bridge.render("https://example.com/missing")

    @pytest.mark.asyncio
    async def test_render_api_failure(self) -> None:
        bridge = BrowserRenderingBridge(_make_config())
        mock_resp = _mock_response(
            200,
            json_data={"success": False, "error": "rate limited"},
        )
        bridge._client.post = AsyncMock(return_value=mock_resp)

        with pytest.raises(BrowserRenderingError, match="rate limited"):
            await bridge.render("https://example.com")

    @pytest.mark.asyncio
    async def test_render_http_error(self) -> None:
        bridge = BrowserRenderingBridge(_make_config())
        bridge._client.post = AsyncMock(side_effect=httpx.ConnectError("connection refused"))

        with pytest.raises(BrowserRenderingError, match="HTTP error"):
            await bridge.render("https://example.com")


# ---------------------------------------------------------------------------
# scrape()
# ---------------------------------------------------------------------------


class TestScrape:
    @pytest.mark.asyncio
    async def test_scrape_success(self) -> None:
        bridge = BrowserRenderingBridge(_make_config())
        mock_resp = _mock_response(
            200,
            json_data={
                "elements": [
                    {"text": "Article 1", "href": "/article-1"},
                    {"text": "Article 2", "href": "/article-2"},
                ],
            },
        )
        bridge._client.post = AsyncMock(return_value=mock_resp)

        result = await bridge.scrape("https://example.com", selector=".article")
        assert result.url == "https://example.com"
        assert result.selector == ".article"
        assert len(result.elements) == 2
        assert result.elements[0]["text"] == "Article 1"

    @pytest.mark.asyncio
    async def test_scrape_custom_attributes(self) -> None:
        bridge = BrowserRenderingBridge(_make_config())
        mock_resp = _mock_response(
            200,
            json_data={"elements": [{"data-id": "42"}]},
        )
        bridge._client.post = AsyncMock(return_value=mock_resp)

        result = await bridge.scrape(
            "https://example.com",
            selector="div",
            attributes=["data-id"],
        )
        assert result.elements[0]["data-id"] == "42"

    @pytest.mark.asyncio
    async def test_scrape_empty_results(self) -> None:
        bridge = BrowserRenderingBridge(_make_config())
        mock_resp = _mock_response(200, json_data={"elements": []})
        bridge._client.post = AsyncMock(return_value=mock_resp)

        result = await bridge.scrape("https://example.com", selector=".nonexistent")
        assert result.elements == []


# ---------------------------------------------------------------------------
# screenshot()
# ---------------------------------------------------------------------------


class TestScreenshot:
    @pytest.mark.asyncio
    async def test_screenshot_returns_png_bytes(self) -> None:
        bridge = BrowserRenderingBridge(_make_config())
        png_bytes = b"\x89PNG\r\n\x1a\nfake-image-data"
        encoded = base64.b64encode(png_bytes).decode()
        mock_resp = _mock_response(200, json_data={"screenshot": encoded})
        bridge._client.post = AsyncMock(return_value=mock_resp)

        result = await bridge.screenshot("https://example.com")
        assert result == png_bytes

    @pytest.mark.asyncio
    async def test_screenshot_full_page(self) -> None:
        bridge = BrowserRenderingBridge(_make_config())
        png_bytes = b"full-page-image"
        encoded = base64.b64encode(png_bytes).decode()
        mock_resp = _mock_response(200, json_data={"screenshot": encoded})
        bridge._client.post = AsyncMock(return_value=mock_resp)

        result = await bridge.screenshot("https://example.com", full_page=True)
        assert result == png_bytes

        # Verify fullPage was sent in the payload
        call_args = bridge._client.post.call_args
        payload = call_args.kwargs.get("json") or call_args.args[1]
        assert payload["fullPage"] is True


# ---------------------------------------------------------------------------
# pdf()
# ---------------------------------------------------------------------------


class TestPdf:
    @pytest.mark.asyncio
    async def test_pdf_returns_bytes(self) -> None:
        bridge = BrowserRenderingBridge(_make_config())
        pdf_bytes = b"%PDF-1.4 fake-pdf-content"
        encoded = base64.b64encode(pdf_bytes).decode()
        mock_resp = _mock_response(200, json_data={"pdf": encoded})
        bridge._client.post = AsyncMock(return_value=mock_resp)

        result = await bridge.pdf("https://example.com")
        assert result == pdf_bytes

    @pytest.mark.asyncio
    async def test_pdf_api_error(self) -> None:
        bridge = BrowserRenderingBridge(_make_config())
        mock_resp = _mock_response(500, content=b"internal server error")
        bridge._client.post = AsyncMock(return_value=mock_resp)

        with pytest.raises(BrowserRenderingError, match="500"):
            await bridge.pdf("https://example.com")


# ---------------------------------------------------------------------------
# execute_script()
# ---------------------------------------------------------------------------


class TestExecuteScript:
    @pytest.mark.asyncio
    async def test_execute_script_returns_result(self) -> None:
        bridge = BrowserRenderingBridge(_make_config())
        mock_resp = _mock_response(
            200,
            json_data={"result": {"count": 42, "items": ["a", "b"]}},
        )
        bridge._client.post = AsyncMock(return_value=mock_resp)

        result = await bridge.execute_script(
            "https://example.com",
            "return document.querySelectorAll('a').length",
        )
        assert result == {"count": 42, "items": ["a", "b"]}

    @pytest.mark.asyncio
    async def test_execute_script_returns_primitive(self) -> None:
        bridge = BrowserRenderingBridge(_make_config())
        mock_resp = _mock_response(200, json_data={"result": 42})
        bridge._client.post = AsyncMock(return_value=mock_resp)

        result = await bridge.execute_script(
            "https://example.com",
            "return 42",
        )
        assert result == 42

    @pytest.mark.asyncio
    async def test_execute_script_returns_none(self) -> None:
        bridge = BrowserRenderingBridge(_make_config())
        mock_resp = _mock_response(200, json_data={})
        bridge._client.post = AsyncMock(return_value=mock_resp)

        result = await bridge.execute_script(
            "https://example.com",
            "console.log('hello')",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_execute_script_error(self) -> None:
        bridge = BrowserRenderingBridge(_make_config())
        bridge._client.post = AsyncMock(side_effect=httpx.ConnectError("connection refused"))

        with pytest.raises(BrowserRenderingError, match="HTTP error"):
            await bridge.execute_script("https://example.com", "return 1")


# ---------------------------------------------------------------------------
# Error attributes
# ---------------------------------------------------------------------------


class TestBrowserRenderingError:
    def test_error_attributes(self) -> None:
        err = BrowserRenderingError(
            "something failed",
            url="https://example.com",
            status_code=502,
        )
        assert str(err) == "something failed"
        assert err.url == "https://example.com"
        assert err.status_code == 502

    def test_error_defaults(self) -> None:
        err = BrowserRenderingError("basic error")
        assert err.url is None
        assert err.status_code is None
