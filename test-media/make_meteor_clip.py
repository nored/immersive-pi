#!/usr/bin/env python3
"""Render a deterministic, cylindrical 360-degree asteroid fly-by panorama.

Requires numpy, Pillow and ffmpeg. One full panorama is cropped by the existing
room player; --nodes only controls optional diagnostic labels. No AI assets.
"""
import argparse
import math
from pathlib import Path
import subprocess
import numpy as np
from PIL import Image, ImageDraw
from make_ring_clip import font


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--width', type=int, default=1920)
    ap.add_argument('--height', type=int, default=1080)
    ap.add_argument('--fps', type=int, default=30)
    ap.add_argument('--seconds', type=int, default=30)
    ap.add_argument('--nodes', type=int, default=12)
    ap.add_argument('--out', type=Path, default=Path(__file__).with_name('meteor-flyby-360.mp4'))
    a = ap.parse_args()
    if min(a.width, a.height, a.fps, a.seconds, a.nodes) <= 0 or a.width % 2 or a.height % 2:
        ap.error('positive dimensions/counts and even video dimensions required')
    w, h = a.width, a.height
    rng = np.random.default_rng(4812)
    # Low-poly, irregular closed spheres. Their lighting changes with rotation.
    vertices = [(0, 0, 1)]
    rings, sides = 6, 12
    for j in range(1, rings):
        lat = math.pi * j / rings
        for k in range(sides):
            ang = 2 * math.pi * k / sides
            r = rng.uniform(.76, 1.21)
            vertices.append((r * math.sin(lat) * math.cos(ang), r * math.sin(lat) * math.sin(ang), r * math.cos(lat)))
    vertices.append((0, 0, -1))
    v = np.array(vertices)
    faces = []
    for k in range(sides):
        faces.append((0, 1+k, 1+(k+1)%sides))
        for j in range(rings-2):
            p, q = 1+j*sides+k, 1+j*sides+(k+1)%sides
            faces.extend(((p, p+sides, q), (q, p+sides, q+sides)))
        faces.append((len(v)-1, 1+(rings-2)*sides+(k+1)%sides, 1+(rings-2)*sides+k))
    faces = np.array(faces)
    face_tint = rng.uniform(.72, 1.15, len(faces))
    background = Image.new('RGB', (w, h), (2, 4, 10))
    d = ImageDraw.Draw(background)
    for _ in range(1800):
        x, y = int(rng.integers(w)), int(rng.integers(h))
        brightness = int(rng.integers(45, 160))
        d.point((x, y), fill=(brightness, brightness, min(255, brightness+30)))
    rocks = []
    for i in range(38):
        rocks.append((rng.uniform(0, 2*math.pi), rng.uniform(0, 1),
                      rng.uniform(2.8, 7), rng.uniform(-2.4, 2.4),
                      rng.uniform(.22, .7), rng.uniform(.3, 1.1), rng.uniform(0, 6)))
    text_font = font(max(10, min(int(w/a.nodes/13), h//40)))
    encoders = []
    for path in (a.out, a.out.with_name(a.out.stem+'-diagnostic.mp4')):
        encoders.append(subprocess.Popen(['ffmpeg','-hide_banner','-loglevel','error','-y',
            '-f','rawvideo','-pix_fmt','rgb24','-s',f'{w}x{h}','-r',str(a.fps),'-i','pipe:0',
            '-an','-c:v','libx264','-preset','fast','-crf','19','-profile:v','high',
            '-pix_fmt','yuv420p','-movflags','+faststart',str(path)], stdin=subprocess.PIPE))
    light = np.array([-.3, -.4, .86]); light /= np.linalg.norm(light)
    try:
        for f in range(a.seconds*a.fps):
            t = f/a.fps
            image = background.copy(); draw = ImageDraw.Draw(image)
            polygons = []
            for heading, phase, impact, z, size, spin, seed in rocks:
                # Periodic straight fly-bys, fading only when far away. No jump at loop.
                u = ((t/a.seconds+phase) % 1)*2-1
                travel = u*44
                fade = min(1., (1-abs(u))*8)
                center = np.array([impact*math.cos(heading)-travel*math.sin(heading),
                                   impact*math.sin(heading)+travel*math.cos(heading), z])
                # Integer full rotations make orientation periodic too.
                angle = round(spin*3)*2*math.pi*t/a.seconds + seed
                c,s = math.cos(angle), math.sin(angle)
                rotation = np.array([[c,-s,0],[s,c,0],[0,0,1]])
                points = v @ rotation.T * size + center
                center_az = math.atan2(center[1], center[0])
                az = np.arctan2(points[:,1], points[:,0])
                az = center_az + (az-center_az+math.pi)%(2*math.pi)-math.pi
                # Cylindrical projection: all horizontal bearings around the viewer.
                px = (az/(2*math.pi)+.5)*w
                py = h*.5 - points[:,2]/np.linalg.norm(points[:,:2],axis=1)*h*.85
                for j, face in enumerate(faces):
                    tri = points[face]
                    normal = np.cross(tri[1]-tri[0], tri[2]-tri[0])
                    normal /= max(np.linalg.norm(normal), 1e-9)
                    if np.dot(normal, tri.mean(axis=0)) >= 0:
                        continue
                    shade = (.22+.78*max(0., np.dot(normal,light)))*face_tint[j]*fade
                    color = tuple(int(min(255, b*shade+3)) for b in (159,146,128))
                    polygons.append((float(np.linalg.norm(tri.mean(axis=0))), [(px[k],py[k]) for k in face], color))
            for _, polygon, color in sorted(polygons, key=lambda p:p[0], reverse=True):
                for offset in (-w,0,w):
                    coords = [(x+offset,y) for x,y in polygon]
                    if max(x for x,y in coords)>=0 and min(x for x,y in coords)<w:
                        draw.polygon(coords, fill=color)
            encoders[0].stdin.write(image.tobytes())
            for i in range(a.nodes):
                x = i*w/a.nodes
                draw.line((x,0,x,h), fill=(40,90,110), width=1)
                cx = (i+.5)*w/a.nodes
                for y, text in ((.05, f'SECTOR {i+1:02d}'),(.92,f'F {f:05d}'),(.96,f'{t:06.2f}s')):
                    draw.text((cx,h*y),text,font=text_font,fill='white',stroke_width=1,stroke_fill='black',anchor='mm')
            encoders[1].stdin.write(image.tobytes())
            if f % (a.fps*5)==0:
                print(f'Rendered {t:.0f}/{a.seconds}s',flush=True)
            if f == a.fps*5:
                image.save(a.out.with_suffix('.jpg'))
    finally:
        for encoder in encoders:
            encoder.stdin.close()
        codes = [encoder.wait() for encoder in encoders]
    if any(codes):
        raise SystemExit(f'Encoding failed: {codes}')
    print(f'Wrote {a.out} and diagnostic version',flush=True)


if __name__ == '__main__':
    main()
