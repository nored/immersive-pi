#!/usr/bin/env bash
# make_pan_clip.sh — worst-case content for the scanout-phase seam test.
#
# A 2-minute 1080p H.264 clip: a hard vertical bar and a fine numbered grid
# panning horizontally fast enough to expose tearing at a seam. A free-running
# HDMI scanout phase shows up as the bar stepping as it crosses the overlap.
# This is the clip you run at the first two-node install.
#
# Output: pan.mp4 (H.264 High, yuv420p) next to this script.
#
# Usage:  ./make_pan_clip.sh [seconds] [width] [height] [fps] [pan_px_per_s]
set -euo pipefail

DUR="${1:-120}"
W="${2:-1920}"
H="${3:-1080}"
FPS="${4:-60}"
SPEED="${5:-1200}"          # bar/grid horizontal speed in px/s — fast on purpose
OUT="$(cd "$(dirname "$0")" && pwd)/pan.mp4"

# A grid wider than the frame so it can scroll continuously, numbered every cell
# so you can name exactly which column steps at the seam. Built as a filtergraph:
#   - mid-grey base
#   - vertical + horizontal grid lines every 64 px
#   - a hard white vertical bar
#   - column numbers burned in
#   - the whole thing scrolled left at SPEED px/s, wrapping seamlessly
#
# We render onto a canvas 2*W wide and crop a W-wide moving window so the scroll
# wraps with no visible seam in the content itself (only the wall can add one).
CW=$((W * 2))
FONT="${FONT:-/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf}"
[ -f "$FONT" ] || FONT="$(fc-match -f '%{file}' mono 2>/dev/null || echo "")"

# Build the grid-number drawtext chain: a label at the top of every 64px column.
NUMS=""
i=0
while [ $((i * 64)) -lt "$CW" ]; do
  x=$((i * 64 + 4))
  NUMS="${NUMS}drawtext=fontfile='${FONT}':text='${i}':x=${x}:y=8:fontsize=18:fontcolor=white,"
  i=$((i + 1))
done

ffmpeg -y \
  -f lavfi -i "color=c=0x404040:s=${CW}x${H}:r=${FPS}:d=${DUR}" \
  -filter_complex "
    [0:v]
    drawgrid=w=64:h=64:t=1:c=0x808080@1.0,
    drawbox=x=$((CW/2)):y=0:w=6:h=${H}:color=white@1.0:t=fill,
    ${NUMS}
    format=yuv420p,
    crop=${W}:${H}:x='mod(t*${SPEED}\\,${W})':y=0,
    setsar=1
  " \
  -c:v libx264 -profile:v high -pix_fmt yuv420p -preset medium -crf 18 \
  -r "${FPS}" -t "${DUR}" \
  "${OUT}"

echo "wrote ${OUT}"
echo "play it at the two-node install; stand at the overlap and watch the white bar cross the seam."
