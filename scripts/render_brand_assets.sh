#!/usr/bin/env bash
# render_brand_assets.sh - export the PNG side of the brand kit from its SVGs.
#
# The SVGs under docs/assets/brand/ are the source (scripts/gen_brand_svgs.py
# writes them). This script rasterises the sizes that GitHub, PyPI, the docs
# site, the VS Code marketplace and chat clients ask for. rsvg-convert is the
# one renderer we rely on because it honours the luminance mask that cuts the
# prompt out of the mark; browsers do too, some SVG-to-PNG shortcuts do not.
#
#   brew install librsvg     # macOS
#   apt install librsvg2-bin # Debian/Ubuntu
#
# Usage, from the repository root:  scripts/render_brand_assets.sh
set -euo pipefail

cd "$(dirname "$0")/.."
BRAND=docs/assets/brand
command -v rsvg-convert >/dev/null || { echo "rsvg-convert not found (librsvg)" >&2; exit 1; }

render() { # <svg> <width> <out.png> [extra rsvg args]
  local svg=$1 w=$2 out=$3; shift 3
  rsvg-convert -w "$w" "$@" "$svg" -o "$out"
  echo "wrote $out"
}

# Mark on transparent, the padded 800 canvas (viewBox 0 0 800 800).
for s in 32 64 128 256 512 1024; do
  render "$BRAND/bernstein-mark.svg" "$s" "$BRAND/bernstein-mark-$s.png"
done

# Square logos on the token backgrounds - avatars, app icons, marketplaces.
for s in 512 1024; do
  render "$BRAND/bernstein-logo-square-dark.svg" "$s" "$BRAND/bernstein-logo-square-dark-$s.png"
done
render "$BRAND/bernstein-logo-square-light.svg" 512 "$BRAND/bernstein-logo-square-light-512.png"

# Link previews.
render "$BRAND/bernstein-social-1280x640.svg" 1280 "$BRAND/bernstein-social-1280x640.png"
render "$BRAND/bernstein-og-1200x630.svg" 1200 "$BRAND/bernstein-og-1200x630.png"

# VS Code marketplace icon (package.json "icon"): 128 px, opaque dark square.
render "$BRAND/bernstein-logo-square-dark.svg" 128 packages/vscode/media/bernstein-icon.png
