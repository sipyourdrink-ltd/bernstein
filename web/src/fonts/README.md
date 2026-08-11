# Vendored webfonts

The dashboard's two typefaces are committed here rather than fetched at view
time. Declarations live in [`../index.css`](../index.css); Vite fingerprints
these files into `src/bernstein/gui/static/assets/`, which the wheel already
ships (`artifacts` in `pyproject.toml`).

## Why they are committed

A CSS `@import` of the Google Fonts stylesheet blocks its own stylesheet from
finishing, and that stylesheet blocks the `<script type="module">` after it. On
a host with no route to `fonts.googleapis.com` the page commits,
`document.readyState` stays `interactive`, `DOMContentLoaded` never fires and
`#root` stays empty. The failure is a blank dashboard, not a substituted
typeface, so the `system-ui` fallback in the font stack never gets to help.

Two further reasons, the same ones that strip the CDN webfont from
`docs/assets/tui-live.svg` — see
[`docs/contributing/render-freshness.md`](../../../docs/contributing/render-freshness.md):
this project ships an air-gap profile (`.github/workflows/airgap-e2e.yml`), and
an operator opening the dashboard would otherwise announce themselves to a
third party on every view.

`tests/unit/test_webui_no_external_assets.py` fails if an external host comes
back into the shipped CSS, and if a `url()` in it stops resolving to a file
that is actually in the bundle.

## What is here

Both families are **variable** fonts: one file covers every weight the UI asks
for, so this is four files rather than the eight static instances the old
`@import` requested (`wght@300;400;500;600;700` + `wght@400;500;600`).

Subsets are `latin` and `latin-ext`. Text outside those ranges — Cyrillic,
Greek, Vietnamese — falls back to `system-ui` / `ui-monospace`, which is a font
mismatch in a task title, not a broken page.

| File | Family | Weights | Subset | Bytes | sha256 |
|---|---|---|---|---|---|
| `inter-tight-v9-latin.woff2` | Inter Tight | 300–700 | latin | 44,916 | `83d548cd73ef2e039167db3adb5ea9d7a7870466ffc8a162c9820bc348938aaf` |
| `inter-tight-v9-latin-ext.woff2` | Inter Tight | 300–700 | latin-ext | 89,820 | `3c299662298bcf2cbf119996f900acce3782695a35e584bc22a566c5d6ea8b48` |
| `jetbrains-mono-v24-latin.woff2` | JetBrains Mono | 400–600 | latin | 31,340 | `2c32b9b3ee358c119e210f6f5195f9bd34894d78a785ff2e95d60e718e400af4` |
| `jetbrains-mono-v24-latin-ext.woff2` | JetBrains Mono | 400–600 | latin-ext | 11,596 | `9c38cb2d0d2d93c1ee6e21fa78db76f13ea7e15e15cc64214c7ca89b6aaa35c4` |

Total 177,672 bytes, served from the same origin as the rest of the bundle.

## Provenance

Captured 2026-08-10 from the Google Fonts CSS API, which is what the removed
`@import` resolved to. The stylesheet that named these files:

```
https://fonts.googleapis.com/css2?family=Inter+Tight:wght@300..700&family=JetBrains+Mono:wght@400..600&display=swap
```

requested with a Chrome user agent, since the API varies its response by
client. The `unicode-range` values in `../index.css` are copied verbatim from
that response, so subsetting behaves exactly as it did before.

| File | Upstream |
|---|---|
| `inter-tight-v9-latin.woff2` | `https://fonts.gstatic.com/s/intertight/v9/NGSwv5HMAFg6IuGlBNMjxLsH8ahuQ2e8.woff2` |
| `inter-tight-v9-latin-ext.woff2` | `https://fonts.gstatic.com/s/intertight/v9/NGSwv5HMAFg6IuGlBNMjxLsJ8ahuQ2e8Smg.woff2` |
| `jetbrains-mono-v24-latin.woff2` | `https://fonts.gstatic.com/s/jetbrainsmono/v24/tDbv2o-flEEny0FZhsfKu5WU4zr3E_BX0PnT8RD8yKwBNntkaToggR7BYRbKPxDcwgknk-4.woff2` |
| `jetbrains-mono-v24-latin-ext.woff2` | `https://fonts.gstatic.com/s/jetbrainsmono/v24/tDbv2o-flEEny0FZhsfKu5WU4zr3E_BX0PnT8RD8yKwBNntkaToggR7BYRbKPx7cwgknk-6nFg.woff2` |

The `v9` / `v24` in each filename is the upstream font version, so a refresh
lands as a new filename next to the old one rather than a silent byte swap.

## Licence

Both families are under the SIL Open Font License 1.1 — see [`OFL.txt`](OFL.txt),
which carries both copyright notices. The OFL is a permissive font licence and
does not affect the Apache-2.0 grant over the rest of the project; it does
require the licence to travel with the font files, which is why `OFL.txt` sits
in this directory.
