"""Cloudflare Browser Rendering bridge for agent web browsing.

Enables Bernstein agents to browse web pages, take screenshots,
and extract structured content using Cloudflare's Browser Rendering API.
"""

from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)


def _str_list() -> list[str]:
    return []


def _str_any_dict() -> dict[str, Any]:
    return {}


def _dict_list() -> list[dict[str, str]]:
    return []


@dataclass(frozen=True)
class BrowserConfig:
    """Configuration for Cloudflare Browser Rendering.

    Attributes:
        account_id: Cloudflare account identifier.
        api_token: Cloudflare API token with Browser Rendering permissions.
        timeout_seconds: HTTP request timeout for all browser calls.
        viewport_width: Browser viewport width in pixels.
        viewport_height: Browser viewport height in pixels.
        user_agent: User-Agent string sent by the headless browser.
        block_ads: Whether to block ad-related requests.
        javascript_enabled: Whether to execute JavaScript on pages.
    """

    account_id: str
    api_token: str
    timeout_seconds: int = 30
    viewport_width: int = 1280
    viewport_height: int = 720
    user_agent: str = "BernsteinBot/1.0"
    block_ads: bool = True
    javascript_enabled: bool = True


@dataclass(frozen=True)
class PageResult:
    """Result of rendering a web page.

    Attributes:
        url: The URL that was rendered.
        title: Page title extracted from the document.
        content: Extracted text content of the page.
        html: Full HTML source if requested.
        screenshot_base64: Base64-encoded PNG screenshot if requested.
        status_code: HTTP status code of the page load.
        load_time_ms: Page load time in milliseconds.
        links: List of href URLs found on the page.
        metadata: Additional metadata from the rendering response.
    """

    url: str
    title: str
    content: str
    html: str = ""
    screenshot_base64: str = ""
    status_code: int = 200
    load_time_ms: float = 0.0
    links: list[str] = field(default_factory=_str_list)
    metadata: dict[str, Any] = field(default_factory=_str_any_dict)


@dataclass(frozen=True)
class ScrapedData:
    """Structured data extracted from a page.

    Attributes:
        url: The URL that was scraped.
        selector: CSS selector used to find elements.
        elements: List of dicts with extracted attribute values per element.
    """

    url: str
    selector: str
    elements: list[dict[str, str]] = field(default_factory=_dict_list)


class BrowserRenderingError(Exception):
    """Raised when a Browser Rendering API call fails.

    Attributes:
        url: The URL the error is associated with, if applicable.
        status_code: HTTP status code returned by the API, if applicable.
    """

    def __init__(
        self,
        message: str,
        *,
        url: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.url = url
        self.status_code = status_code


class BrowserRenderingBridge:
    """Bridge to Cloudflare Browser Rendering API.

    Provides web browsing capabilities to Bernstein agents: page rendering,
    screenshot capture, PDF generation, content scraping, and JavaScript
    execution on rendered pages.

    Usage::

        browser = BrowserRenderingBridge(BrowserConfig(
            account_id="...", api_token="..."
        ))
        page = await browser.render("https://example.com")
        print(page.title, page.content[:500])

        data = await browser.scrape("https://example.com", selector=".article")
    """

    def __init__(self, config: BrowserConfig) -> None:
        """Initialise the browser rendering bridge.

        Args:
            config: Browser rendering configuration.

        Raises:
            BrowserRenderingError: If required configuration fields are missing.
        """
        if not config.account_id:
            raise BrowserRenderingError("BrowserConfig requires a non-empty account_id")
        if not config.api_token:
            raise BrowserRenderingError("BrowserConfig requires a non-empty api_token")
        self._config = config
        self._client = httpx.AsyncClient(
            headers=self._build_headers(),
            timeout=httpx.Timeout(float(config.timeout_seconds)),
        )

    @property
    def config(self) -> BrowserConfig:
        """The configuration this bridge was initialised with."""
        return self._config

    async def render(
        self,
        url: str,
        *,
        screenshot: bool = False,
        full_html: bool = False,
    ) -> PageResult:
        """Render a web page and extract content.

        Args:
            url: URL to render.
            screenshot: If True, include base64-encoded screenshot.
            full_html: If True, include full HTML in result.

        Returns:
            PageResult with extracted title, content, links, and optionally
            screenshot and HTML.

        Raises:
            BrowserRenderingError: If the API call fails or returns an error.
        """
        payload: dict[str, Any] = {
            "url": url,
            "viewport": {
                "width": self._config.viewport_width,
                "height": self._config.viewport_height,
            },
            "userAgent": self._config.user_agent,
            "blockAds": self._config.block_ads,
            "javascript": self._config.javascript_enabled,
            "extractContent": True,
            "extractLinks": True,
            "screenshot": screenshot,
            "fullHtml": full_html,
        }

        start = time.monotonic()
        data = await self._request("render", payload, url=url)
        elapsed_ms = (time.monotonic() - start) * 1000

        return PageResult(
            url=url,
            title=data.get("title", ""),
            content=data.get("content", ""),
            html=data.get("html", "") if full_html else "",
            screenshot_base64=data.get("screenshot", "") if screenshot else "",
            status_code=data.get("statusCode", 200),
            load_time_ms=data.get("loadTimeMs", elapsed_ms),
            links=data.get("links", []),
            metadata=data.get("metadata", {}),
        )

    async def scrape(
        self,
        url: str,
        *,
        selector: str,
        attributes: list[str] | None = None,
    ) -> ScrapedData:
        """Extract structured data from page elements matching CSS selector.

        Args:
            url: URL to scrape.
            selector: CSS selector for target elements.
            attributes: Element attributes to extract (default: text, href, src).

        Returns:
            ScrapedData with extracted elements.

        Raises:
            BrowserRenderingError: If the API call fails or returns an error.
        """
        attrs = attributes or ["text", "href", "src"]
        payload: dict[str, Any] = {
            "url": url,
            "selector": selector,
            "attributes": attrs,
            "viewport": {
                "width": self._config.viewport_width,
                "height": self._config.viewport_height,
            },
            "userAgent": self._config.user_agent,
            "javascript": self._config.javascript_enabled,
        }

        data = await self._request("scrape", payload, url=url)

        return ScrapedData(
            url=url,
            selector=selector,
            elements=data.get("elements", []),
        )

    async def screenshot(self, url: str, *, full_page: bool = False) -> bytes:
        """Take a screenshot of a web page, return PNG bytes.

        Args:
            url: URL to capture.
            full_page: If True, capture the full scrollable page.

        Returns:
            PNG image bytes.

        Raises:
            BrowserRenderingError: If the API call fails or returns an error.
        """
        payload: dict[str, Any] = {
            "url": url,
            "fullPage": full_page,
            "viewport": {
                "width": self._config.viewport_width,
                "height": self._config.viewport_height,
            },
            "userAgent": self._config.user_agent,
            "javascript": self._config.javascript_enabled,
        }

        data = await self._request("screenshot", payload, url=url)
        encoded = data.get("screenshot", "")
        return base64.b64decode(encoded)

    async def pdf(self, url: str) -> bytes:
        """Generate PDF of a web page.

        Args:
            url: URL to render as PDF.

        Returns:
            PDF file bytes.

        Raises:
            BrowserRenderingError: If the API call fails or returns an error.
        """
        payload: dict[str, Any] = {
            "url": url,
            "viewport": {
                "width": self._config.viewport_width,
                "height": self._config.viewport_height,
            },
            "userAgent": self._config.user_agent,
            "javascript": self._config.javascript_enabled,
        }

        data = await self._request("pdf", payload, url=url)
        encoded = data.get("pdf", "")
        return base64.b64decode(encoded)

    async def execute_script(self, url: str, script: str) -> Any:
        """Execute JavaScript on a rendered page and return result.

        Args:
            url: URL to render before executing the script.
            script: JavaScript code to execute in the page context.

        Returns:
            The value returned by the script (JSON-serialisable).

        Raises:
            BrowserRenderingError: If the API call fails or script errors.
        """
        payload: dict[str, Any] = {
            "url": url,
            "script": script,
            "viewport": {
                "width": self._config.viewport_width,
                "height": self._config.viewport_height,
            },
            "userAgent": self._config.user_agent,
            "javascript": True,
        }

        data = await self._request("execute", payload, url=url)
        return data.get("result")

    async def _request(
        self,
        endpoint: str,
        payload: dict[str, Any],
        *,
        url: str = "",
    ) -> dict[str, Any]:
        """Send a request to the Browser Rendering API.

        Args:
            endpoint: API endpoint name (e.g. "render", "screenshot").
            payload: JSON request body.
            url: Original target URL for error reporting.

        Returns:
            Parsed JSON response body.

        Raises:
            BrowserRenderingError: On HTTP errors or non-success responses.
        """
        api_url = self._api_url(endpoint)
        try:
            resp = await self._client.post(api_url, json=payload)
        except httpx.TimeoutException as exc:
            raise BrowserRenderingError(
                f"Timeout rendering {url}: {exc}",
                url=url,
            ) from exc
        except httpx.HTTPError as exc:
            raise BrowserRenderingError(
                f"HTTP error rendering {url}: {exc}",
                url=url,
            ) from exc

        if resp.status_code >= 400:
            raise BrowserRenderingError(
                f"Browser Rendering API returned {resp.status_code}: {resp.text}",
                url=url,
                status_code=resp.status_code,
            )

        data: dict[str, Any] = resp.json()
        if not data.get("success", True):
            raise BrowserRenderingError(
                f"Browser Rendering failed for {url}: {data.get('error', 'unknown')}",
                url=url,
            )
        return data

    def _build_headers(self) -> dict[str, str]:
        """Build API request headers."""
        return {
            "Authorization": f"Bearer {self._config.api_token}",
            "Content-Type": "application/json",
        }

    def _api_url(self, endpoint: str) -> str:
        """Build Browser Rendering API URL.

        Args:
            endpoint: API endpoint name.

        Returns:
            Full API URL string.
        """
        return f"https://api.cloudflare.com/client/v4/accounts/{self._config.account_id}/browser-rendering/{endpoint}"
