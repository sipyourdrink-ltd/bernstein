"""Rendering source fetcher for the research modality (#3120).

The static fetcher records the bytes the server returns. For a growing share
of sources those bytes do not contain the text a claim cites, because the
text only exists after client-side rendering. ``ResearchWorker.run``
content-addresses whatever ``fetch_fn`` returns and binds each claim's span
to it; if the stored bytes are a shell document, the citation resolves to a
hash of bytes that do not contain the cited sentence, and verification still
passes because the bytes are intact. The binding is meaningless anyway.

This module provides a ``fetch_fn``-compatible callable
(``Callable[[str], bytes]``) that returns the rendered DOM at fetch time,
fixing the claim-to-bytes binding at capture time -- the only place it can
be fixed.

Two invariants, in order of importance:

1. A fetch that cannot render **raises** a typed refusal naming the source
   ref. It never returns empty bytes: ``activity.fetch`` content-addresses
   whatever comes back, and empty bytes would be hashed, stored, and become
   a perfectly valid citation target -- the exact silent failure this
   feature exists to prevent, reintroduced by the fix.
2. The rendering backend is an optional extra
   (``bernstein[research-render]``, the ``playwright`` package). When it is
   absent the fetcher refuses with a typed unavailable error naming the
   exact package, so the failure is explicit instead of a bare
   ``ImportError`` at fetch time. (Playwright was chosen over crawl4ai
   because crawl4ai's transitive deps carry CNRI-Python / LGPL-2.1-only
   licences that fail the repo's dependency-review gate; both wrap the same
   headless Chromium.)
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


class RenderingFetchError(RuntimeError):
    """A source could not be rendered for capture.

    Raised instead of returning empty bytes, so a failed render can never
    become a content-addressed citation target.
    """


class RenderingBackendUnavailableError(RuntimeError):
    """The rendering backend (``playwright``) is not installed.

    Install ``bernstein[research-render]`` to enable rendering fetches.
    """


def make_rendering_fetcher() -> Callable[[str], bytes]:
    """Build a ``Callable[[str], bytes]`` returning the rendered DOM.

    The backend is imported lazily so importing this module never pulls a
    browser engine into the process; the first call refuses with a typed
    unavailable error when ``playwright`` is not installed. The returned
    callable is synchronous (the seam ``ResearchWorker`` uses), runs its own
    event loop, and must therefore not be invoked from inside a running loop.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover - exercised via sys.modules patch
        raise RenderingBackendUnavailableError(
            "rendering fetch requires the optional extra "
            "'bernstein[research-render]' (package 'playwright'); "
            "install it and retry"
        ) from exc

    def fetch(source_ref: str) -> bytes:
        if not source_ref:
            raise RenderingFetchError("cannot render empty source ref")
        try:
            rendered = _render(async_playwright, source_ref)
        except RenderingFetchError:
            raise
        except Exception as exc:  # playwright raises several ad-hoc error types
            raise RenderingFetchError(f"failed to render {source_ref!r}: {exc}") from exc
        if not rendered:
            raise RenderingFetchError(f"rendering {source_ref!r} produced empty content")
        return rendered

    return fetch


def _render(playwright_api: type, source_ref: str) -> bytes:
    """Render ``source_ref`` once and return the DOM as UTF-8 bytes.

    The headless browser is created and closed per call so a failed render
    cannot leak a half-open backend into the next fetch.
    """

    async def _once() -> str:
        async with playwright_api() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(source_ref, timeout=30_000)
                return await page.content()
            finally:
                await browser.close()

    # Wall-clock cap: a hung render must surface as a typed refusal, not a
    # worker that blocks forever on a single source.
    return asyncio.run(asyncio.wait_for(_once(), timeout=45.0)).encode("utf-8", errors="replace")
