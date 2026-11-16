#!/usr/bin/env python3
"""Make a separate tiled diagnostic package at an exact rational frame rate.

Preserves every source frame and region. This deliberately changes playback
duration; use for cadence experiments, not automatic production conversion.
"""
import argparse
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('outdir', type=Path)
    parser.add_argument('--fps', type=Fraction, required=True)
    args = parser.parse_args()
    if args.fps <= 0:
        parser.error('fps must be positive')
    package = json.loads(args.manifest.read_text())
    rate = args.fps
    suffix = f'cadence-{rate.numerator}-{rate.denominator}'
    stem = args.manifest.stem.removesuffix('.immersive')
    destination = args.outdir / f'{stem}-{suffix}.immersive.json'
    outputs = [(node, spec, args.outdir / f'{Path(spec["file"]).stem}-{suffix}.mp4')
               for node, spec in package['nodes'].items()]
    if destination.exists() or any(path.exists() for _, _, path in outputs):
        parser.error('experiment outputs already exist; choose a fresh directory')
    args.outdir.mkdir(parents=True, exist_ok=True)
    for node, spec, output in outputs:
        source = args.manifest.parent / spec['file']
        if hashlib.sha256(source.read_bytes()).hexdigest() != spec['sha256']:
            raise ValueError(f'Source checksum mismatch: {source}')
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-n',
                        '-i', str(source), '-an', '-vf',
                        f'setpts=N*{rate.denominator}/({rate.numerator}*TB)',
                        '-r', str(rate), '-c:v', 'libx264', '-threads', '2',
                        '-preset', 'fast', '-crf', '18', '-profile:v', 'high',
                        '-pix_fmt', 'yuv420p', '-video_track_timescale', str(rate.numerator),
                        '-movflags', '+faststart', str(output)], check=True)
        probe = json.loads(subprocess.check_output([
            'ffprobe', '-v', 'error', '-select_streams', 'v:0',
            '-show_entries', 'stream=width,height,nb_frames,avg_frame_rate',
            '-of', 'json', str(output)], text=True))['streams'][0]
        if (int(probe['nb_frames']) != package['frames'] or
                Fraction(probe['avg_frame_rate']) != rate or
                probe['width'] != spec['width'] or probe['height'] != spec['height']):
            raise ValueError(f'Output verification failed: {output}: {probe}')
        spec['file'] = output.name
        spec['sha256'] = hashlib.sha256(output.read_bytes()).hexdigest()
        print(f'{node}: {probe}', flush=True)
    package['fps_num'], package['fps_den'] = rate.numerator, rate.denominator
    destination.write_text(json.dumps(package, indent=2) + '\n')
    print(destination)


if __name__ == '__main__':
    main()
