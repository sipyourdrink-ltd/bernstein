# Brand and logo

One mark, one wordmark, one set of files. This page is the source of truth for
how Bernstein is drawn: the geometry, the colours, where each file is used, and
how to regenerate all of it from one script. The design tokens the identity sits
on are in [Design tokens](tokens.md).

## The mark

A pointy-top hexagon with the terminal prompt `>_` cut through it.

| Element | Meaning |
|---|---|
| Hexagon | amber - the stone the project is named after; a cell in a governed structure |
| `>_` cut-out | the shell where agents run; it is a hole, not a drawing, so the background shows through |
| Upper-left facet | one lighter band, the light on a polished face; never more than one |

Geometry (800 x 800 master, `docs/assets/brand/bernstein-mark.svg`): circumradius 240, corner radius 50, the prompt centred at x = 400. The tight box is the inner 640 x 640 (`viewBox="80 80 640 640"`). Everything else is derived from this file by `scripts/gen_brand_svgs.py`.

## The wordmark

The word **bernstein**, lower case, set in Fraunces at weight 500, to the right of the mark, cap height aligned to the mark's centre. The wordmark is shipped as outlines, so it renders the same on GitHub, PyPI and in mail clients that load no web fonts.

Two files, chosen by colour scheme:

| Surface | File | Text colour |
|---|---|---|
| dark | `docs/assets/logo-dark.svg` | `#F2EFE6` (token `text`, dark) |
| light | `docs/assets/logo-light.svg` | `#181614` (token `text`, light) |

The README picks one with `<picture>` + `prefers-color-scheme`. **Those two paths are hotlinked** from the PyPI project page, the website repository and third-party listings via `raw.githubusercontent.com/.../main/docs/assets/logo-{dark,light}.svg`. Change their content; never their name or location.

## Colours

| Role | Hex | Where |
|---|---|---|
| Amber | `#F5A524` | the mark, and nothing else in the UI |
| Amber facet | `#FFC96A` | the one light band on the mark |
| Dark background | `#13130F` | token `bg` (dark): square logos, app icons, favicons on dark |
| Paper background | `#F8F1E7` | token `bg` (light): social previews, square logo on light |
| Text | `#181614` / `#F2EFE6` | token `text`: wordmark |

Amber is the identity colour only. The product's accent stays terracotta (`#A35B48` / `#DB8E7A`, see tokens): the mark is the one warm-yellow thing on any page, which is what makes it findable.

## Rules

- **Clear space**: at least the height of the `_` around the mark on every side.
- **Minimum size**: 16 px for the mark alone (the favicon), 120 px wide for the wordmark.
- **One colour**: use `bernstein-mark-mono.svg` (`currentColor`) where colour is not available - the VS Code activity bar, monochrome print, embossing.
- **Backgrounds**: transparent mark on any token background; square logos where a platform needs an opaque tile.
- Do not rotate, outline, add a drop shadow, recolour the amber, fill the `>_`, or place the mark on a photograph.
- Do not draw the wordmark in another typeface; use the SVG.

## Files

All identity files live in `docs/assets/brand/` (plus the two hotlink-stable wordmarks one level up). Names are `bernstein-<what>[-<variant>][-<size>].<ext>`, lower-case, hyphenated.

| File | Use |
|---|---|
| `bernstein-mark.svg` | the master: transparent, padded 800 x 800 |
| `bernstein-mark-mono.svg` | one-colour mark, `currentColor`, tight box |
| `bernstein-mark-{32,64,128,256,512,1024}.png` | transparent PNG exports of the mark |
| `bernstein-logo-square-dark.svg`, `bernstein-logo-square-dark-{512,1024}.png` | opaque tile on `#13130F`: GitHub org/repo avatar, marketplaces, app icons |
| `bernstein-logo-square-light.svg`, `bernstein-logo-square-light-512.png` | the same tile on paper |
| `bernstein-social-1280x640.{svg,png}` | GitHub repository social preview |
| `bernstein-og-1200x630.{svg,png}` | `og:image` for the docs site |
| `../logo-dark.svg`, `../logo-light.svg` | wordmark, hotlinked - keep the paths |

Where the identity is wired in:

| Surface | File |
|---|---|
| README header | `README.md` (`<picture>` of the two wordmarks); `banner-readme.webp` stays as the hero |
| Docs site logo + favicon | `mkdocs.yml` (`theme.logo`, `theme.favicon` → `assets/brand/bernstein-mark.svg`) |
| Docs `og:image` | `docs/overrides/main.html`, `docs/benchmarks/leaderboard.html` |
| Operator GUI favicon | `web/index.html` (inline SVG data URI of the mark) |
| PWA icons 192 / 512 | `src/bernstein/gui/pwa.py::render_icon_png` - the same geometry rasterised in pure Python, maskable-safe |
| VS Code extension | `packages/vscode/media/bernstein-icon.svg` (mono, activity bar) and `bernstein-icon.png` (128 px, marketplace) |
| Terminal | `docs/assets/ascii_logo.md` - the text-only wordmark for the CLI splash |

## Regenerating

```bash
uv run --with 'fonttools[woff]' python scripts/gen_brand_svgs.py \
    --fraunces /path/to/Fraunces-variable.woff2 --geist /path/to/Geist-Regular.ttf
scripts/render_brand_assets.sh      # PNG exports, needs rsvg-convert (librsvg)
```

The fonts are not vendored: Fraunces (OFL) from Google Fonts, Geist (OFL) ships with `@vercel/og`. `tests/unit/test_brand_assets.py` checks that the files, names and sizes listed here exist and match.
