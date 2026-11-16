#!/usr/bin/env python3
"""Render native per-projector asteroid fly-bys on a shared cylindrical timeline.

Requires NumPy, Pillow and ffmpeg. Optional room-model source windows include
physical overlap in each native-size tile; calibration is never applied here.
"""
import argparse
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path
import subprocess

import numpy as np
from PIL import Image, ImageDraw
from make_ring_clip import font
from panorama_tiles import load_layout, source_size, tile_images


class Scene:
    def __init__(self, width, height, nodes, frames, panorama_width=None):
        self.width = panorama_width if panorama_width is not None else width * nodes
        self.height, self.frames = height, frames
        rng = np.random.default_rng(4812)
        rings, sides = 6, 12
        vertices = [(0, 0, 1)]
        for j in range(1, rings):
            latitude = math.pi * j / rings
            for k in range(sides):
                angle, radius = 2 * math.pi * k / sides, rng.uniform(.76, 1.21)
                vertices.append((radius * math.sin(latitude) * math.cos(angle),
                                 radius * math.sin(latitude) * math.sin(angle), radius * math.cos(latitude)))
        vertices.append((0, 0, -1))
        self.vertices = np.array(vertices)
        faces = []
        for k in range(sides):
            faces.append((0, 1 + k, 1 + (k + 1) % sides))
            for j in range(rings - 2):
                p, q = 1 + j * sides + k, 1 + j * sides + (k + 1) % sides
                faces.extend(((p, p + sides, q), (q, p + sides, q + sides)))
            faces.append((len(vertices) - 1, 1 + (rings - 2) * sides + (k + 1) % sides,
                          1 + (rings - 2) * sides + k))
        self.faces = np.array(faces)
        self.tint = rng.uniform(.72, 1.15, len(faces))
        self.light = np.array([-.3, -.4, .86]); self.light /= np.linalg.norm(self.light)
        self.background = Image.new('RGB', (self.width, height), (2, 4, 10))
        stars = np.random.default_rng(4813)
        draw = ImageDraw.Draw(self.background)
        for _ in range(nodes * 800):
            x, y = int(stars.random() * self.width), int(stars.random() * height)
            brightness = int(stars.integers(45, 180))
            draw.point((x, y), fill=(brightness, brightness, min(255, brightness + 30)))
        rocks = np.random.default_rng(4814)
        self.rocks = []
        for i in range(38):
            size = rocks.uniform(.25, .8)
            clearance = rocks.uniform(.25, 1.1) if i % 4 == 0 else rocks.uniform(2, 6)
            self.rocks.append((rocks.uniform(0, 2 * math.pi), rocks.random(),
                               1.4 * size + clearance, rocks.uniform(-.65, .65),
                               size, int(rocks.integers(1, 4)), rocks.uniform(0, 2 * math.pi)))

    def render(self, frame):
        phase = (frame % self.frames) / self.frames
        scale = self.width / (2 * math.pi)
        polygons = []
        for heading, offset, impact, z, size, turns, seed in self.rocks:
            travel_phase = ((phase + offset) % 1) * 2 - 1
            alpha = round(min(1., (1 - abs(travel_phase)) * 8) * 255)
            if alpha == 0:
                continue
            travel = travel_phase * 44
            center = np.array([impact * math.cos(heading) - travel * math.sin(heading),
                               impact * math.sin(heading) + travel * math.cos(heading), z])
            angle = turns * 2 * math.pi * phase + seed
            c, s = math.cos(angle), math.sin(angle)
            points = self.vertices @ np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]]).T * size + center
            center_azimuth = math.atan2(center[1], center[0])
            azimuth = np.arctan2(points[:, 1], points[:, 0])
            azimuth = center_azimuth + (azimuth - center_azimuth + math.pi) % (2 * math.pi) - math.pi
            projected = np.column_stack(((azimuth + math.pi) * scale,
                self.height / 2 - points[:, 2] / np.linalg.norm(points[:, :2], axis=1) * scale))
            triangles = points[self.faces]
            normals = np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
            normals /= np.maximum(np.linalg.norm(normals, axis=1, keepdims=True), 1e-9)
            centers = triangles.mean(axis=1)
            visible = np.einsum('ij,ij->i', normals, centers) < 0
            shades = (.22 + .78 * np.maximum(0., normals @ self.light)) * self.tint
            colors = np.minimum(255, shades[:, None] * np.array([159, 146, 128]) + 3).astype(np.uint8)
            depths = np.linalg.norm(centers, axis=1)
            for index in np.flatnonzero(visible):
                polygons.append((depths[index], projected[self.faces[index]],
                                 tuple(map(int, colors[index])) + (alpha,)))
        image = self.background.copy()
        draw = ImageDraw.Draw(image, 'RGBA')
        for _, polygon, color in sorted(polygons, key=lambda p: p[0], reverse=True):
            for offset in (-self.width, 0, self.width):
                if polygon[:, 0].max() + offset >= 0 and polygon[:, 0].min() + offset < self.width:
                    draw.polygon([(float(x + offset), float(y)) for x, y in polygon], fill=color)
        return image


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--nodes', type=int)
    parser.add_argument('--room-model', type=Path,
                        help='Use this model\'s node IDs and overlapping source regions; does not change room calibration')
    parser.add_argument('--width', type=int, default=1280)
    parser.add_argument('--height', type=int, default=800)
    parser.add_argument('--frames', type=int, default=720)
    parser.add_argument('--fps', type=Fraction, default=Fraction(521875, 17451))
    parser.add_argument('--diagnostic', action='store_true')
    parser.add_argument('--outdir', type=Path, default=Path(__file__).with_name('native-meteor-1280'))
    args = parser.parse_args()
    if min(args.width, args.height, args.frames, args.fps) <= 0 or args.width % 2 or args.height % 2:
        parser.error('positive counts and even video dimensions required')
    try:
        layout = load_layout(args.room_model, args.nodes)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    args.nodes = len(layout)
    args.outdir.mkdir(parents=True, exist_ok=True)
    prefix = f'native-meteor-{args.width}x{args.height}' + ('-mapped' if args.room_model else '') + ('-diagnostic' if args.diagnostic else '')
    paths = [args.outdir / f'{prefix}-node-{node+1:02d}.mp4' for node in range(args.nodes)]
    manifest = args.outdir / f'{prefix}.immersive.json'
    if manifest.exists() or any(path.exists() for path in paths):
        parser.error('output already exists; choose a new directory')
    source_width, source_height = source_size(layout, args.width, args.height)
    scene = Scene(args.width, source_height, args.nodes, args.frames, panorama_width=source_width)
    encoders = []
    label = font(max(18, args.height // 28))
    try:
        for path in paths:
            encoders.append(subprocess.Popen(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-n',
                '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', f'{args.width}x{args.height}',
                '-r', str(args.fps), '-i', 'pipe:0', '-an', '-c:v', 'libx264', '-threads', '1',
                '-preset', 'fast', '-crf', '18', '-profile:v', 'high', '-level:v', '4.1',
                '-maxrate', '8M', '-bufsize', '16M', '-pix_fmt', 'yuv420p',
                '-video_track_timescale', str(args.fps.numerator), '-movflags', '+faststart', str(path)],
                stdin=subprocess.PIPE))
        for frame in range(args.frames):
            panorama = scene.render(frame)
            tiles = tile_images(panorama, layout, args.width, args.height)
            for node, (encoder, tile) in enumerate(zip(encoders, tiles)):
                if args.diagnostic:
                    draw = ImageDraw.Draw(tile)
                    draw.text((args.width / 2, args.height * .08), layout[node]['node'],
                              font=label, fill='white', stroke_width=2, stroke_fill='black', anchor='mm')
                    draw.text((args.width / 2, args.height * .92), f'FRAME {frame:05d}',
                              font=label, fill='white', stroke_width=2, stroke_fill='black', anchor='mm')
                encoder.stdin.write(tile.tobytes())
                if frame == args.frames // 4:
                    tile.save(args.outdir / f'preview-node-{node+1:02d}.jpg', quality=95)
            if frame % 120 == 0:
                print(f'Rendered {frame}/{args.frames} frames on {args.nodes} native tiles', flush=True)
    finally:
        for encoder in encoders:
            if encoder.stdin:
                try: encoder.stdin.close()
                except BrokenPipeError: pass
        codes = [encoder.wait() for encoder in encoders]
    if len(codes) != args.nodes or any(codes):
        raise RuntimeError(f'Encoding failed: {codes}')
    package = {'format': 'immersive-tiled-v1', 'fps_num': args.fps.numerator,
               'fps_den': args.fps.denominator, 'frames': args.frames, 'nodes': {}}
    for node, path in enumerate(paths):
        probe = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
            '-show_entries', 'stream=width,height,nb_frames,avg_frame_rate', '-of', 'json', str(path)], text=True))['streams'][0]
        assert (probe['width'], probe['height'], int(probe['nb_frames']), Fraction(probe['avg_frame_rate'])) == (args.width, args.height, args.frames, args.fps), probe
        package['nodes'][layout[node]['node']] = {'file': path.name,
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'width': args.width, 'height': args.height,
            'region': layout[node]['region']}
    manifest.write_text(json.dumps(package, indent=2) + '\n')
    print(f'Wrote {manifest}', flush=True)


if __name__ == '__main__':
    main()
