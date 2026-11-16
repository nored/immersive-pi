#!/usr/bin/env python3
"""Convert selected downloaded catalog sources into full native room packages."""
import argparse
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import subprocess
import sys


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(4*1024*1024), b''):
            digest.update(block)
    return digest.hexdigest()


def video_duration(metadata):
    """Use picture duration, excluding an audio tail or a nonzero track start."""
    stream = metadata['streams'][0]
    if stream.get('duration') not in (None, 'N/A'):
        return Fraction(stream['duration'])
    start = Fraction(stream.get('start_time') or '0')
    end = stream.get('tags', {}).get('DURATION')
    if end:
        hours, minutes, seconds = end.split(':')
        return int(hours)*3600 + int(minutes)*60 + Fraction(seconds) - start
    return Fraction(metadata['format']['duration']) - start


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('catalog', type=Path)
    parser.add_argument('--source-root', type=Path, required=True)
    parser.add_argument('--models', type=Path, required=True)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--ids', nargs='+', required=True)
    args = parser.parse_args()
    catalog = {row['id']: row for row in json.loads(args.catalog.read_text())['sources']}
    if len(set(args.ids)) != len(args.ids) or not set(args.ids) <= catalog.keys():
        parser.error('Source IDs must be distinct catalog entries')
    args.output.mkdir(parents=True, exist_ok=False)
    report = {'jobs': [], 'status': 'running', 'models_sha256': sha256(args.models),
              'baseline_sha256': sha256(args.baseline)}
    def save():
        (args.output/'conversion-report.json').write_text(json.dumps(report, indent=2)+'\n')
    save()
    try:
        for source_id in args.ids:
            row = catalog[source_id]
            source = (args.source_root/row.get('directory', '.')/row['file']).resolve()
            if not source.is_relative_to(args.source_root.resolve()):
                raise ValueError('Source escapes the library')
            metadata = json.loads(subprocess.check_output(['ffprobe','-v','error','-select_streams','v:0',
                '-show_entries','stream=duration,start_time:stream_tags=DURATION:format=duration',
                '-of','json',str(source)]))
            fps = Fraction(521875,17451)
            frames = int(video_duration(metadata)*fps)
            directory = args.output/source_id
            job = {'id': source_id, 'status': 'rendering', 'frames': frames,
                   'source_sha256': sha256(source), 'output': str(directory)}
            report['jobs'].append(job);save()
            renderer = Path(__file__).resolve().parents[1]/'phone-capture/make_capture_meteor.py'
            with (args.output/(source_id+'.log')).open('w') as log:
                subprocess.run([sys.executable,'-u',str(renderer),'--models',str(args.models),
                    '--baseline',str(args.baseline),'--output',str(directory),'--frames',str(frames),
                    '--video',str(source),'--source-projection',row.get('projection','equirect'),
                    '--gpu-map','--hardware-video'],stdout=log,stderr=subprocess.STDOUT,check=True)
            original = next(directory.glob('*.immersive.json'))
            package = json.loads(original.read_text())
            package['title'] = row['title']
            package['attribution'] = {key: row[key] for key in
                ('creator','license','license_url','source_page','attribution')}
            package['attribution']['changes'] = ('Full visual timeline, projection conversion when required, '
                'room geometry mapping and provisional blending, 1280x800 per projector, '
                'frame-rate resampling, silent output.')
            manifest = directory/(source_id+'-full-mapped.immersive.json')
            manifest.write_text(json.dumps(package,indent=2)+'\n')
            if original != manifest:original.unlink()
            mapping_path = directory/'mapping-report.json'
            mapping = json.loads(mapping_path.read_text());mapping['manifest']=manifest.name
            mapping_path.write_text(json.dumps(mapping,indent=2)+'\n')
            job.update(status='converted_not_player_validated',manifest=str(manifest))
            save();print(json.dumps(job),flush=True)
        report['status']='converted_not_player_validated';save()
    except BaseException as error:
        report['status']='failed';report['error']=str(error);save();raise


if __name__ == '__main__':
    main()
