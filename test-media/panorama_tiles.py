"""Generate tiled media coverage from explicit room-model source windows."""
import json
import math
from pathlib import Path

from PIL import Image


def load_layout(room_model=None, nodes=None):
    if room_model is None:
        nodes = 12 if nodes is None else nodes
        if type(nodes) is not int or nodes < 1:
            raise ValueError('nodes must be a positive integer')
        return [{'node':f'dortmund-slave-{i+1}',
                 'region':{'x':i/nodes,'y':0,'w':1/nodes,'h':1}}
                for i in range(nodes)]
    model = json.loads(Path(room_model).read_text())
    if not isinstance(model, dict):
        raise ValueError('room model must be an object')
    entries = model.get('nodes')
    if not isinstance(entries, list) or not entries:
        raise ValueError('room model must contain node entries')
    if nodes is not None and nodes != len(entries):
        raise ValueError('--nodes disagrees with the room model')
    result, seen = [], set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError('room model entries must be objects')
        node, region = entry.get('node'), entry.get('source_region')
        if not isinstance(node, str) or not node.strip() or node in seen:
            raise ValueError('room model node IDs must be nonempty and unique')
        if not isinstance(region, dict) or not all(
            type(region.get(k)) in (int, float) and math.isfinite(region[k])
            for k in ('x','y','w','h')):
            raise ValueError(f'{node}: source region must contain finite coordinates')
        if type(region.get('wrap', False)) is not bool:
            raise ValueError(f'{node}: wrap must be boolean')
        if not 0 < region['w'] <= 1 or not 0 < region['h'] <= 1:
            raise ValueError(f'{node}: source dimensions must be within 0–1')
        if region['y'] < 0 or region['y'] + region['h'] > 1 + 1e-9:
            raise ValueError(f'{node}: vertical source region is outside the panorama')
        if not region.get('wrap', False) and (region['x'] < 0 or region['x'] + region['w'] > 1 + 1e-9):
            raise ValueError(f'{node}: source region crosses the seam without wrap')
        seen.add(node)
        result.append({'node':node,'region':{k:region[k] for k in ('x','y','w','h','wrap') if k in region}})
    return result


def source_size(layout, width, height):
    """Keep at least one source pixel for every output pixel in every tile."""
    return (math.ceil(width / min(e['region']['w'] for e in layout) - 1e-9),
            math.ceil(height / min(e['region']['h'] for e in layout) - 1e-9))


def tile_images(panorama, layout, width, height):
    """Crop identical panorama content into adjacent overlapping source windows.

    Periodic padding also supplies the resampling filter across the 360° seam.
    It is built once per frame, rather than once per projector.
    """
    pw, ph = panorama.size
    periodic = None
    if any(e['region'].get('wrap', False) for e in layout):
        periodic = Image.new(panorama.mode, (pw * 3, ph))
        for x in (0, pw, pw * 2):
            periodic.paste(panorama, (x, 0))
    tiles = []
    for entry in layout:
        r = entry['region']
        wrap = r.get('wrap', False)
        x = ((r['x'] % 1) + 1) * pw if wrap else r['x'] * pw
        bounds = (x, r['y'] * ph, x + r['w'] * pw, (r['y'] + r['h']) * ph)
        image = periodic if wrap else panorama
        integral = tuple(round(v) for v in bounds)
        if (all(abs(a-b) < 1e-7 for a,b in zip(bounds, integral)) and
                integral[2]-integral[0] == width and integral[3]-integral[1] == height):
            tile = image.crop(integral)
        else:
            tile = image.transform((width, height), Image.Transform.EXTENT, bounds,
                                   resample=Image.Resampling.BICUBIC)
        tiles.append(tile)
    return tiles
