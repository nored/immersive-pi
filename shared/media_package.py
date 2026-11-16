"""Resolve one video or a set of per-node videos with one frame timeline."""
import hashlib
import json
import math
from pathlib import Path
import re

FORMAT = 'immersive-tiled-v1'


def load_plan(directory, name, members):
    directory = Path(directory)
    if Path(name).name != name or not name:
        raise ValueError('media must be a plain filename')
    path = directory / name
    payload = path.read_bytes() if name.endswith('.immersive.json') else None
    if payload is None:
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        return {'sha256': digest, 'spec': None,
                'nodes': {n: {'file': name, 'sha256': digest, 'region': None} for n in members}}
    package = json.loads(payload)
    if package.get('format') != FORMAT:
        raise ValueError('unsupported tiled-video manifest format')
    spec = {k: package.get(k) for k in ('fps_num', 'fps_den', 'frames')}
    if not all(type(v) is int and v > 0 for v in spec.values()):
        raise ValueError('manifest needs a positive rational frame rate and frame count')
    assets = {}
    for node in members:
        asset = package.get('nodes', {}).get(node)
        if not isinstance(asset, dict):
            raise ValueError(f'video package has no tile for {node}')
        filename, digest = asset.get('file'), asset.get('sha256', '')
        if not isinstance(filename, str) or Path(filename).name != filename or not filename.endswith('.mp4'):
            raise ValueError(f'{node} tile must be a plain MP4 filename')
        if not re.fullmatch('[0-9a-f]{64}', digest):
            raise ValueError(f'{node} tile has an invalid content hash')
        if not (directory / filename).is_file():
            raise ValueError(f'{node} tile is missing: {filename}')
        if any(type(asset.get(k)) is not int or asset[k] <= 0 for k in ('width', 'height')):
            raise ValueError(f'{node} tile dimensions are invalid')
        region = asset.get('region')
        if not isinstance(region, dict) or not all(type(region.get(k)) in (int,float) and math.isfinite(region[k]) for k in ('x','y','w','h')):
            raise ValueError(f'{node} tile has an invalid panorama region')
        if region['w'] <= 0 or region['h'] <= 0:
            raise ValueError(f'{node} tile region must have positive dimensions')
        assets[node] = {k: asset[k] for k in ('file','sha256','width','height','region')}
    return {'sha256': hashlib.sha256(payload).hexdigest(), 'spec': spec, 'nodes': assets}


def tile_uv(u, v, region):
    """Map global panorama coordinates into the prepared node tile."""
    if region is None:
        return u, v
    return (u-region['x'])/region['w'], (v-region['y'])/region['h']


def validate_tile_coverage(entry, region):
    """Reject source windows/mesh UVs that would clamp outside a node's clip."""
    if region is None:
        return  # A shared panorama has no per-node crop boundary.
    source = entry['source_region']
    # Include the full declared source window, even if today's mesh samples
    # only part of it. Resampling the mesh must not reveal missing tile pixels.
    points = [(0,0), (1,1)] + [(p['u'],p['v']) for p in entry['mesh']['points']]
    for u,v in points:
        u,v = tile_uv(source['x']+u*source['w'], source['y']+v*source['h'], region)
        if not all(math.isfinite(c) and -1e-7 <= c <= 1+1e-7 for c in (u,v)):
            raise ValueError(f"{entry['node']}: video tile does not cover the requested source region; "
                             "stop playback and generate matching overlapping tiles before starting again")
