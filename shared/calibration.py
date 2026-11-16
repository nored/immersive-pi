"""Validate calibration candidates before they can replace working geometry."""
import json
import math


def validate_field(field, value):
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    try:
        json.dumps(value, allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise ValueError(f'{field} contains invalid or non-finite values') from exc
    def number(v):
        return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
    if field == 'source_region':
        if not all(number(value.get(k)) for k in ('x', 'y', 'w', 'h')) or min(value['w'], value['h']) <= 0:
            raise ValueError('source region must have finite coordinates and positive dimensions')
    elif field == 'mesh':
        cols, rows = value.get('cols'), value.get('rows')
        if not isinstance(cols, int) or not isinstance(rows, int) or not 2 <= cols <= 64 or not 2 <= rows <= 64:
            raise ValueError('mesh dimensions must be between 2 and 64')
        points = value.get('points', [])
        if len(points) != cols*rows:
            raise ValueError('mesh control-point count does not match its dimensions')
        if not all(all(number(p.get(k)) for k in ('u', 'v', 'x', 'y')) for p in points):
            raise ValueError('mesh control points must be finite numbers')
        for axes in (('u', 'v'), ('x', 'y')):
            signs = []
            for r in range(rows-1):
                for c in range(cols-1):
                    i = r*cols+c
                    for ids in ((i, i+1, i+cols+1), (i, i+cols+1, i+cols)):
                        a,b,d = [points[j] for j in ids]
                        x,y = axes
                        area = (b[x]-a[x])*(d[y]-a[y])-(b[y]-a[y])*(d[x]-a[x])
                        if abs(area) < 1e-9:
                            raise ValueError('calibration contains a collapsed mesh triangle')
                        signs.append(area > 0)
            if len(set(signs)) != 1:
                raise ValueError('calibration contains a folded mesh')
    elif field == 'color':
        if not number(value.get('gamma')) or value['gamma'] <= 0:
            raise ValueError('colour gamma must be positive')
        for name in ('gain', 'lift'):
            if len(value.get(name, [])) != 3 or not all(number(v) for v in value[name]):
                raise ValueError(f'colour {name} must contain three finite numbers')
    elif field == 'blend':
        for edge in ('left','right','top','bottom'):
            e = value.get(edge, {})
            if not all(number(e.get(k, 0)) for k in ('width', 'gamma', 'black_lift')):
                raise ValueError('blend values must be finite')
            if not 0 <= e.get('width', 0) <= 1 or e.get('gamma', 2.2) <= 0:
                raise ValueError('blend widths must be within 0–1 and gamma must be positive')
    else:
        raise ValueError('unsupported calibration field')


def validate_solution(entries, expected_nodes):
    if sorted(e.get('node') for e in entries) != sorted(expected_nodes):
        raise ValueError('calibration result does not match the selected nodes')
    for entry in entries:
        for field in ('source_region', 'mesh', 'blend'):
            validate_field(field, entry[field])
