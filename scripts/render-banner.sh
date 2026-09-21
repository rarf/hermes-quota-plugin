#!/usr/bin/env bash
# Regenerate the README crops and the 2:1 catalog banner from a real Hermes
# Desktop screenshot.
#
#   usage: scripts/render-banner.sh <screenshot.png> <outdir> [version]
#
# The screenshot is a full app window captured at 2x (2626x1626); the crop
# coordinates below assume that layout — the quota pane's left border sits at
# x=2021, its header starts at y=143 and the pane's own "fetched …" footer ends
# before the global status bar at y=1588. Re-measure them for a different window
# size (a 1px-wide colour scan down a row/column finds the pane borders).
#
# Needs ImageMagick (`magick`) and the Adwaita fonts.
set -Eeuo pipefail

SRC="${1:?screenshot path}"
OUT="${2:?output dir}"
VERSION="${3:-2.4.0}"
mkdir -p "$OUT"

# Pane: a sliver of workspace on the left for a natural edge, from just under
# the app's tab strip to just above the global status bar.
magick "$SRC" -crop 632x1460+1990+120 +repage "$OUT/quota-pane.png"

# Full window, trimmed of the 4px window frame.
magick "$SRC" -shave 4x4 "$OUT/quota-app.png"

# --- banner: 1200x600 (2:1), the shape the plugin catalog expects -------------
H=520
R=16
magick "$OUT/quota-pane.png" -resize x${H} /tmp/_pane.png
W=$(magick identify -format "%w" /tmp/_pane.png)
magick -size ${W}x${H} xc:none -fill white \
  -draw "roundrectangle 0,0,$((W-1)),$((H-1)),$R,$R" /tmp/_mask.png
magick /tmp/_pane.png /tmp/_mask.png -alpha off -compose CopyOpacity -composite \
  -alpha set -stroke '#3d4757' -strokewidth 2 -fill none \
  -draw "roundrectangle 1,1,$((W-2)),$((H-2)),$R,$R" /tmp/_pane-card.png
magick /tmp/_pane-card.png \( +clone -background black -shadow 70x10+0+8 \) \
  +swap -background none -layers merge +repage /tmp/_pane-shadow.png

magick -size 1200x600 gradient:'#141b25-#0a0d11' \
  -font Adwaita-Sans -pointsize 19 -fill '#93a0b0' \
  -annotate +62+70 'HERMES DESKTOP PLUGIN' \
  -font Adwaita-Sans-Bold -pointsize 84 -fill '#f4f6f8' \
  -annotate +60+168 'Hermes Quota' \
  -font Adwaita-Sans -pointsize 30 -fill '#a7b2c1' \
  -annotate +62+226 'Per-provider quota and rate limits,' \
  -annotate +62+266 'in the desktop pane and the status bar.' \
  -pointsize 21 -fill '#8c98a8' \
  -annotate +62+328 'Copilot · OpenAI Codex · OpenCode Go · Grok · Gemini' \
  -annotate +62+360 'Anthropic · Kimi · Nous Portal · OpenRouter · Z.ai' \
  -pointsize 20 -fill '#6d7887' \
  -annotate +62+524 "v${VERSION}  ·  community plugin  ·  github.com/rarf/hermes-quota-plugin" \
  /tmp/_bg.png

magick /tmp/_bg.png /tmp/_pane-shadow.png -gravity NorthEast -geometry +44+40 \
  -composite "$OUT/quota-banner.png"

rm -f /tmp/_pane.png /tmp/_mask.png /tmp/_pane-card.png /tmp/_pane-shadow.png /tmp/_bg.png
identify "$OUT"/quota-banner.png "$OUT"/quota-pane.png "$OUT"/quota-app.png
