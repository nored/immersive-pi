#!/usr/bin/env python3
"""Make a panoramic H.264 diagnostic for the existing full-video/crop player.

Requires Pillow and ffmpeg. Sector numbers describe panorama positions, not
node identities; the room model determines which projector displays each one.
"""

import argparse
import colorsys
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def font(size):
    for path in ("/System/Library/Fonts/Menlo.ttc",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"):
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--nodes", type=int, default=12)
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--lap-seconds", type=int, default=24)
    ap.add_argument("--laps", type=int, default=2)
    ap.add_argument("--out", type=Path,
                    default=Path(__file__).with_name("ring-test-12.mp4"))
    args = ap.parse_args()
    if min(args.nodes, args.fps, args.lap_seconds, args.laps) < 1:
        ap.error("nodes, fps, lap-seconds and laps must be positive")
    if args.width < args.nodes * 64 or args.height < 240:
        ap.error("use at least 64 pixels per sector and a height of 240")
    if args.width % 2 or args.height % 2:
        ap.error("H.264 yuv420p needs even width and height")
    if not shutil.which("ffmpeg"):
        ap.error("ffmpeg is required")
    w, h = args.width, args.height
    sector = w / args.nodes
    small = font(max(10, min(int(sector / 13), h // 40)))
    large = font(max(16, min(int(sector / 5), h // 12)))
    background = Image.new("RGB", (w, h))
    draw = ImageDraw.Draw(background)
    for i in range(args.nodes):
        x0, x1 = round(i * sector), round((i + 1) * sector)
        rgb = tuple(round(c * 255) for c in colorsys.hsv_to_rgb(i / args.nodes, .65, .28))
        draw.rectangle((x0, 0, x1 - 1, h - 1), fill=rgb)
        draw.line((x0, 0, x0, h), fill="white", width=2)
        cx = (x0 + x1) // 2
        draw.text((cx, h * .12), f"{i + 1:02d}", font=large, fill="white", anchor="mm")
        draw.text((cx, h * .22), f"{i * 360 / args.nodes:.0f} deg", font=small,
                  fill="white", anchor="mm")
        draw.text((cx, h * .82), "CLOCKWISE >", font=small, fill="white", anchor="mm")
        for y in (.3, .5, .7):
            draw.line((x0, round(h * y), x1, round(h * y)), fill="#808080", width=1)
    frames_per_lap = args.fps * args.lap_seconds
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-f", "rawvideo", "-pixel_format", "rgb24", "-video_size", f"{w}x{h}",
               "-framerate", str(args.fps), "-i", "pipe:0", "-an",
               "-c:v", "libx264", "-preset", "fast", "-crf", "18",
               "-profile:v", "high", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
               str(args.out)]
    # Keep the full panorama 1080p by default: every Pi decodes the whole file.
    with subprocess.Popen(command, stdin=subprocess.PIPE) as encoder:
        try:
            for frame in range(frames_per_lap * args.laps):
                image = background.copy()
                draw = ImageDraw.Draw(image)
                phase = (frame % frames_per_lap) / frames_per_lap
                x = phase * w
                a = max(12, sector * .32)
                cy = h * .5
                # Draw wrapped copies so the arrow crosses the 360/0 seam.
                for offset in (-w, 0, w):
                    cx = x + offset
                    draw.polygon([(cx-a, cy-h*.035), (cx, cy-h*.035),
                                  (cx, cy-h*.09), (cx+a, cy),
                                  (cx, cy+h*.09), (cx, cy+h*.035),
                                  (cx-a, cy+h*.035)], fill="#ffff20")
                    draw.line((cx, h*.3, cx, h*.7), fill="white", width=2)
                # Identical counters in every sector reveal time offsets.
                for i in range(args.nodes):
                    cx = (i + .5) * sector
                    draw.text((cx, h*.9), f"F {frame:05d}", font=small,
                              fill="white", anchor="mm")
                    draw.text((cx, h*.95), f"{frame / args.fps:06.2f}s", font=small,
                              fill="white", anchor="mm")
                encoder.stdin.write(image.tobytes())
        finally:
            encoder.stdin.close()
        if encoder.wait():
            raise SystemExit("ffmpeg encoding failed")
    print(f"Wrote {args.out}: {args.nodes} sectors, {args.laps} clockwise laps")


if __name__ == "__main__":
    main()
