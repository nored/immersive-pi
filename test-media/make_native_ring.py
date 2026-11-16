#!/usr/bin/env python3
"""Make N separate native-resolution diagnostic clips on one 360-degree timeline.

Each file contains 1280x800 useful source pixels by default. The manifest
preserves each tile's global panorama region. This is a timing/resolution test,
not a physical overlap calibration. Requires Pillow and ffmpeg.
"""
import argparse
import colorsys
import hashlib
import json
from pathlib import Path
import subprocess

from PIL import Image, ImageDraw
from make_ring_clip import font


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--nodes', type=int, default=12)
    parser.add_argument('--width', type=int, default=1280)
    parser.add_argument('--height', type=int, default=800)
    parser.add_argument('--fps', type=int, default=30)
    parser.add_argument('--seconds', type=int, default=24)
    parser.add_argument('--outdir', type=Path, default=Path(__file__).with_name('native-ring-1280'))
    args = parser.parse_args()
    if min(args.nodes,args.width,args.height,args.fps,args.seconds) < 1 or args.width%2 or args.height%2:
        parser.error('positive counts and even dimensions are required')
    args.outdir.mkdir(parents=True,exist_ok=True)
    w,h=args.width,args.height
    frames=args.fps*args.seconds
    label=font(max(18,h//30));big=font(h//9)
    backgrounds=[];encoders=[];paths=[]
    package={'format':'immersive-tiled-v1','fps_num':args.fps,'fps_den':1,'frames':frames,'nodes':{}}
    for node in range(args.nodes):
        rgb=tuple(round(v*255) for v in colorsys.hsv_to_rgb(node/args.nodes,.55,.15))
        image=Image.new('RGB',(w,h),rgb);draw=ImageDraw.Draw(image)
        for x in range(0,w,64):draw.line((x,0,x,h),fill='#566477',width=1)
        for y in range(0,h,64):draw.line((0,y,w,y),fill='#566477',width=1)
        draw.rectangle((0,0,w-1,h-1),outline='white',width=2)
        draw.text((w/2,h*.12),f'dortmund-slave-{node+1}',font=big,fill='white',anchor='mm')
        draw.text((w/2,h*.23),f'{w} x {h} SOURCE PIXELS / PROJECTOR',font=label,fill='white',anchor='mm')
        # These unscaled line pairs expose accidentally reusing a tiny panorama crop.
        for band,spacing in enumerate((1,2,4,8)):
            x0=round(w*(.1+band*.2));bw=round(w*.15)
            for x in range(x0,x0+bw,spacing*2):
                draw.rectangle((x,h*.64,x+spacing-1,h*.75),fill='white')
            draw.text((x0+bw/2,h*.79),f'{spacing}px lines',font=label,fill='white',anchor='mm')
        backgrounds.append(image)
        path=args.outdir/f'native-ring-{w}x{h}-node-{node+1:02d}.mp4';paths.append(path)
        command=['ffmpeg','-hide_banner','-loglevel','error','-y','-f','rawvideo',
                 '-pix_fmt','rgb24','-s',f'{w}x{h}','-r',str(args.fps),'-i','pipe:0',
                 '-an','-c:v','libx264','-threads','1','-preset','fast','-crf','18',
                 '-profile:v','high','-pix_fmt','yuv420p','-movflags','+faststart',str(path)]
        encoders.append(subprocess.Popen(command,stdin=subprocess.PIPE))
    try:
        for frame in range(frames):
            arrow=frame/frames*(w*args.nodes)
            for node,(background,encoder) in enumerate(zip(backgrounds,encoders)):
                image=background.copy();draw=ImageDraw.Draw(image)
                for offset in (-w*args.nodes,0,w*args.nodes):
                    cx=arrow-node*w+offset;cy=h*.44;a=w*.09
                    if -2*a < cx < w+2*a:
                        draw.polygon([(cx-a,cy-h*.025),(cx,cy-h*.025),(cx,cy-h*.065),
                                      (cx+a,cy),(cx,cy+h*.065),(cx,cy+h*.025),(cx-a,cy+h*.025)],fill='#ffff30')
                draw.text((w/2,h*.9),f'FRAME {frame:05d}   {frame/args.fps:06.2f}s',
                          font=label,fill='white',anchor='mm')
                encoder.stdin.write(image.tobytes())
                if frame==frames//4 and node==0:image.save(args.outdir/'preview.jpg')
            if frame%(args.fps*4)==0:print(f'Rendered {frame/args.fps:.0f}/{args.seconds}s on {args.nodes} {w}x{h} tiles',flush=True)
    finally:
        for encoder in encoders:encoder.stdin.close()
        codes=[encoder.wait() for encoder in encoders]
    if any(codes):raise SystemExit(f'Encoding failed: {codes}')
    for node,path in enumerate(paths):
        package['nodes'][f'dortmund-slave-{node+1}']={'file':path.name,
            'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'width':w,'height':h,
            'region':{'x':node/args.nodes,'y':0,'w':1/args.nodes,'h':1}}
    manifest=args.outdir/f'native-ring-{w}x{h}.immersive.json'
    manifest.write_text(json.dumps(package,indent=2)+'\n')
    print(f'Wrote {manifest}',flush=True)


if __name__=='__main__':main()
