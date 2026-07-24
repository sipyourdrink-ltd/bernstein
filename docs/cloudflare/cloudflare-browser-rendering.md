# Cloudflare Browser Rendering

Bernstein's `BrowserRenderingBridge` lets agents browse the web. It
wraps Cloudflare's Browser Rendering API - a fleet of headless Chrome
instances on Cloudflare's edge - and exposes the operations that an
agent realistically needs: render a page after JavaScript runs,
extract structured content via CSS selector, take a screenshot,
generate a PDF, and execute arbitrary JavaScript on the rendered page.

This is the doc to read when you want an agent to fetch a docs page,
scrape a competitor's pricing table, or produce a PDF of a rendered
report.

---

## Setup

### Required Cloudflare account configuration

1. A Cloudflare account on the Workers Paid plan (Browser Rendering
   is **not** in the free tier as of writing - check the
   [pricing notes](#cost-and-quota) below before enabling it).
2. Browser Rendering enabled for the account (Dashboard → Workers &
   Pages → Browser Rendering → Enable).
3. An API token scoped to **`Account → Browser Rendering → Edit`**.
   The token is account-scoped, not zone-scoped; it does not need any
   DNS or Workers permissions.

### Environment variables

The bridge reads two values, by convention from environment variables:

| Variable                                                | Purpose                                |
|---------------------------------------------------------|----------------------------------------|
| `CLOUDFLARE_ACCOUNT_ID` (alias `CF_ACCOUNT_ID`)         | Cloudflare account UUID                |
| `CLOUDFLARE_API_TOKEN` (alias `CF_API_TOKEN`)           | API token with Browser Rendering Edit  |

These are the same vars consumed by `cloudflare.py` and
`r2_sync.py`. Set them once and every Cloudflare bridge picks them up.

### Bernstein-side configuration

There is no Browser Rendering section in `bernstein.yaml`. The bridge
is constructed directly from a `BrowserConfig` dataclass:

```python
from bernstein.bridges.browser_rendering import BrowserConfig, BrowserRenderingBridge

cfg = BrowserConfig(
    account_id=os.environ["CLOUDFLARE_ACCOUNT_ID"],
    api_token=os.environ["CLOUDFLARE_API_TOKEN"],
)
browser = BrowserRenderingBridge(cfg)
```

Optional fields (with defaults from `browser_rendering.py:32`):

| Field                | Default              | Notes                                                 |
|----------------------|----------------------|-------------------------------------------------------|
| `timeout_seconds`    | `30`                 | HTTP timeout for every API call                       |
| `viewport_width`     | `1280`               | Pixels                                                |
| `viewport_height`    | `720`                | Pixels                                                |
| `user_agent`         | `BernsteinBot/1.0`   | Sent on every page load                               |
| `block_ads`          | `True`               | Cloudflare-side ad blocking                           |
| `javascript_enabled` | `True`               | Set False for ahead-of-time HTML only                 |

The constructor refuses to start if `account_id` or `api_token` is
empty (`BrowserRenderingError` raised at init time), so missing
credentials fail fast rather than at first call.

---

## Operations

All methods are `async` and return either a typed dataclass
(`PageResult`, `ScrapedData`) or raw bytes. Errors raise
`BrowserRenderingError` with the original URL and HTTP status when
available.

### `render(url, *, screenshot=False, full_html=False)`

Fetches a fully-rendered page after JavaScript has run.

```python
page = await browser.render("https://example.com", full_html=True)
print(page.title)           # "Example Domain"
print(page.content[:200])   # extracted text content
print(len(page.links))      # outbound hrefs
print(page.html[:500])      # only present if full_html=True
```

Returns a `PageResult` with `url`, `title`, `content` (extracted
text), `html` (when `full_html=True`), `screenshot_base64` (when
`screenshot=True`), `status_code`, `load_time_ms`, `links`, and a
free-form `metadata` dict for whatever the API returned that we did
not strongly type.

Use this when the agent needs to *read* what a page actually says.
Static HTTP fetching is faster but breaks on any page that builds its
content with JavaScript.

### `scrape(url, *, selector, attributes=None)`

Extract a structured list of elements matching a CSS selector.

```python
data = await browser.scrape(
    "https://news.ycombinator.com/",
    selector=".titleline > a",
    attributes=["text", "href"],
)
for el in data.elements[:5]:
    print(el["text"], "→", el["href"])
```

Returns `ScrapedData` with a list of dicts, one per matched element.
Defaults to extracting `text`, `href`, and `src`. Pass an explicit
`attributes` list to harvest e.g. `data-*` attributes.

This is materially cheaper than calling `render()` and parsing the
HTML yourself, because Cloudflare runs the selector against the live
DOM after JS, so you never have to ship the full HTML across the
wire.

### `screenshot(url, *, full_page=False)`

Returns PNG bytes.

```python
png = await browser.screenshot("https://example.com", full_page=True)
Path("snapshot.png").write_bytes(png)
```

`full_page=True` captures the entire scrollable page rather than just
the viewport. Useful for documentation and bug reports; expensive on
long pages.

### `pdf(url)`

Returns PDF bytes.

```python
pdf_bytes = await browser.pdf("https://example.com/report")
Path("report.pdf").write_bytes(pdf_bytes)
```

Cloudflare's renderer applies print stylesheets when present, so this
is the right tool for "print the rendered version of this page", not
"save the HTML as PDF".

### `execute_script(url, script)`

Run arbitrary JavaScript in the rendered page context and get the
JSON-serialisable return value.

```python
title = await browser.execute_script(
    "https://example.com",
    "return document.title;",
)
items = await browser.execute_script(
    "https://example.com/list",
    "return [...document.querySelectorAll('li')].map(li => li.innerText);",
)
```

The script must `return` the value you want back; whatever you return
must round-trip through JSON. This is the escape hatch for pages
where `scrape()` is not flexible enough - e.g. paginated lists where
you have to scroll, or content gated behind a click.

Treat `execute_script` as a sandboxed *data* extractor, not a way to
mutate the live page. Anything you write is thrown away when the
browser instance terminates.

---

## Limits

The hard caps come from Cloudflare's Browser Rendering platform.
Bernstein adds one bridge-side default (the request timeout). All
limits below match the Cloudflare docs at the time of writing -
re-check before relying on them in production.

| Constraint                  | Value                                              |
|-----------------------------|----------------------------------------------------|
| Per-request HTTP timeout    | `BrowserConfig.timeout_seconds` (default 30 s)     |
| Page navigation timeout     | 30 s (Cloudflare default; configurable per call)   |
| Maximum concurrent renders  | Account-tier dependent (default ~2 on entry tier)  |
| Maximum browser sessions    | Account-tier dependent                             |
| Allowed protocols           | `http://`, `https://` only                         |
| Page resource cap           | Cloudflare-imposed; large pages may be truncated   |
| Screenshot output           | PNG, base64-encoded in the JSON response           |
| PDF output                  | Cloudflare-imposed page count and size limits      |

The bridge does not enforce per-call rate limiting - it relies on
Cloudflare's API to reject excess concurrent renders with a 4xx,
which the bridge surfaces as a `BrowserRenderingError`. If you are
running many parallel agents that all want to browse, wrap calls in a
local semaphore sized to your account's concurrent-render limit.

If a render exceeds `timeout_seconds`, the bridge raises
`BrowserRenderingError("Timeout rendering ...")` with the original
URL on the exception. The remote browser instance is still cleaned up
by Cloudflare; you do not need a separate teardown call.

---

## Cost and quota

Browser Rendering is billed per browser session minute on Cloudflare
side. Approximate model (verify against the live pricing page before
committing budget):

- A small allotment of free session minutes per month included with
  the Workers Paid plan.
- Additional minutes billed per minute of active browser session, with
  parallel sessions billed independently.
- Storage of returned screenshots and PDFs is on you - typically R2
  or your own object store. The bridge returns bytes; it does not
  upload anywhere.

Two practical things you can do from inside Bernstein:

1. **Track cost via the bridge.** Wrap `render()` / `scrape()` /
   `pdf()` calls in your agent so the orchestrator's cost tracker
   (`core/cost/cost.py`) records browser-render minutes alongside LLM
   tokens. The simplest pattern is a small wrapper that records
   `load_time_ms` from each `PageResult` against a per-task budget.
2. **Cap before it hurts.** A per-task `--budget` already prevents
   runaway LLM spend; if you frequently hit Browser Rendering quota,
   add a per-agent `max_browser_calls` to your task YAML and refuse
   further renders once exceeded.

For real-time visibility, Cloudflare's dashboard surfaces session
counts and minutes under Workers & Pages → Browser Rendering →
Analytics.

---

## Worked example

A small Bernstein agent that fetches a docs page, extracts every
`<h2>` heading, and writes them to a file. This is the shape of
roughly any "browse + extract + persist" task.

```python
import asyncio
import os
from pathlib import Path

from bernstein.bridges.browser_rendering import (
    BrowserConfig,
    BrowserRenderingBridge,
    BrowserRenderingError,
)


async def collect_headings(url: str, out: Path) -> int:
    cfg = BrowserConfig(
        account_id=os.environ["CLOUDFLARE_ACCOUNT_ID"],
        api_token=os.environ["CLOUDFLARE_API_TOKEN"],
        timeout_seconds=20,
    )
    browser = BrowserRenderingBridge(cfg)

    try:
        data = await browser.scrape(
            url,
            selector="h2",
            attributes=["text"],
        )
    except BrowserRenderingError as exc:
        print(f"scrape failed for {exc.url}: {exc}")
        return 1

    headings = [el["text"].strip() for el in data.elements if el.get("text")]
    out.write_text("\n".join(headings), encoding="utf-8")
    print(f"wrote {len(headings)} headings to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(
        asyncio.run(
            collect_headings(
                "https://example.com/docs",
                Path("headings.txt"),
            )
        )
    )
```

Wire this into a Bernstein agent by registering it as a tool in your
plugin and calling it from the agent's task. The bridge takes care of
auth, timeouts, and error mapping; the agent only sees a clean
`ScrapedData` (or a clean exception).

---

## Security boundaries

Rendering arbitrary URLs is, by design, a sandbox. Treat it as one:

1. **Outbound network**. The bridge can be made to fetch anything
   reachable from Cloudflare's edge - internal hostnames included
   when DNS resolves them publicly. If you have private services on
   public DNS, an SSRF-style mistake from an over-eager agent can
   surface them. Mitigations: keep private services on private DNS;
   pre-validate URLs server-side before calling `render()`; refuse
   `file://`, `data:`, and any non-`http(s)` schemes (the API itself
   only accepts `http(s)`, but the agent may still try).

2. **Cookies and session reuse**. Each render starts a fresh browser
   instance. There is no implicit session reuse across calls - if you
   need authenticated browsing, you must inject the cookies via
   `execute_script` or pass them in the request payload.

3. **JavaScript execution**. `execute_script` runs inside Cloudflare's
   isolate, not inside Bernstein. The script cannot reach the
   orchestrator's filesystem, env vars, or vault. The blast radius is
   the page itself plus whatever the page can talk to. Returned
   values are JSON-serialised, so you cannot smuggle out objects with
   side effects.

4. **Returned content is untrusted**. HTML, scraped text, screenshot
   bytes - all of it comes from the rendered URL. Pass it through the
   same sanitisers you would for any user-supplied content before
   feeding it back into prompts, file writes, or DB inserts. The
   default DLP scanner (`core/security/dlp_scanner_v2.py`) is a
   reasonable first line of defence on text payloads.

5. **API token blast radius**. The token used by the bridge has
   account-level Browser Rendering Edit. Compromise of that token
   means an attacker can run renders on your account (mostly a
   billing risk). It does **not** give access to your Workers, R2
   buckets, KV namespaces, or DNS. Rotate it through the Cloudflare
   dashboard if exposed; nothing in the local vault holds it
   unrecoverably (it lives only in env / your secrets manager).

---

## Code pointers

| Concern                         | File                                              |
|---------------------------------|---------------------------------------------------|
| Bridge implementation           | `src/bernstein/bridges/browser_rendering.py`      |
| Common Cloudflare API URL form  | `_api_url()` (same module)                        |
| Auth header construction        | `_build_headers()` (same module)                  |
| Cloudflare bridge family map    | [Cloudflare overview](cloudflare-overview.md)     |
| Sibling bridges                 | [Cloudflare bridges](cloudflare-bridges.md)       |

---

## Related

- [Cloudflare overview](cloudflare-overview.md) - how Browser
  Rendering fits among the other Cloudflare bridges.
- [Cloudflare setup](cloudflare-setup.md) - wrangler, account, and
  token setup that this bridge relies on.
- [Cloudflare bridges](cloudflare-bridges.md) - Workers / Workflow /
  R2 bridges that share the same auth env vars.
- [Secrets and credentials](../operations/secrets.md) - where the
  `CLOUDFLARE_API_TOKEN` lives and how to rotate it.
