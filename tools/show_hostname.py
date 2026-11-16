#!/usr/bin/env python3
"""Show the actual kernel hostname on a node's HDMI output for room mapping.

Run with the renderer stopped and PYOPENGL_PLATFORM=egl. Uses the installed
production display backend, bypassing all panorama cropping and calibration.
"""
import argparse
from pathlib import Path
import signal
import socket
import sys
import time

from PIL import Image, ImageDraw, ImageFont


def make_label(hostname):
    image = Image.new('RGBA', (1280, 720), '#102030')
    draw = ImageDraw.Draw(image)
    draw.rectangle((12, 12, 1267, 707), outline='white', width=8)
    def text(value, center, max_width, height, color):
        font = ImageFont.load_default()
        bbox = font.getbbox(value)
        tile = Image.new('RGBA', (bbox[2]-bbox[0]+4, bbox[3]-bbox[1]+4))
        ImageDraw.Draw(tile).text((2-bbox[0], 2-bbox[1]), value, font=font, fill=color)
        scale = min(max_width/tile.width, height/tile.height)
        tile = tile.resize((round(tile.width*scale), round(tile.height*scale)), Image.Resampling.NEAREST)
        image.alpha_composite(tile, (round(center[0]-tile.width/2), round(center[1]-tile.height/2)))
    text('ACTUAL NODE HOSTNAME', (640, 90), 1000, 60, '#b0dfff')
    text(hostname.rsplit('-', 1)[-1], (640, 320), 900, 300, '#ffff60')
    text(hostname, (640, 555), 1120, 110, 'white')
    text('PHYSICAL PROJECTOR IDENTIFICATION', (640, 650), 1020, 40, '#b0dfff')
    return image


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=int, default=1800)
    parser.add_argument('--mode')
    parser.add_argument('--app-dir', default='/opt/immersive/render-node')
    args = parser.parse_args()
    sys.path.insert(0, args.app_dir)
    from gl_pipeline import KmsDisplay, GLStage
    hostname = socket.gethostname()
    label = make_label(hostname)
    display = KmsDisplay(**({"mode": args.mode} if args.mode else {}))
    w, h = display.setup()
    display.make_current()
    stage = GLStage(w, h, 160, 90)
    stage.init_gl()
    label = label.resize((w,h), Image.Resampling.NEAREST)
    draw = ImageDraw.Draw(label)
    # Native single-pixel stripes stay independent of the scaled hostname text.
    for x in range(30,min(w//4,300),2):
        draw.line((x,h-90,x,h-40),fill="white",width=1)
    stage.upload_raw(label.tobytes(), *label.size)
    running = True
    def stop(_signum, _frame):
        nonlocal running
        running = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    deadline = time.monotonic()+args.seconds
    print(f'Showing actual hostname {hostname} at {w}x{h}', flush=True)
    try:
        while running and time.monotonic() < deadline:
            stage.render_raw()
            display.swap_and_flip()
            time.sleep(.05)
    finally:
        display.teardown()


if __name__ == '__main__':
    main()
