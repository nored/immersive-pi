#!/usr/bin/env python3
"""Export a diagnostic colored point cloud from a verified ARKit capture.

This preserves the recorded poses; it performs no registration, hole filling,
surface fitting or projector calibration. Requires NumPy and Pillow.
"""
import argparse
import io
import json
import math
from pathlib import Path
import shutil
import tempfile

import numpy as np
from PIL import Image

from validate_capture import asset, inspect_bundle, local_file, matrix


VERTEX = np.dtype([('xyz', '<f4', (3,)), ('rgb', 'u1', (3,))])


def unproject(depth, confidence, intrinsics, pose, rgb_size, *, stride=2,
              min_confidence=2, near=0.2, far=8.0):
    """Return world XYZ and depth-grid XY for retained samples.

    Scene depth is axial distance, not Euclidean ray length. Scale native RGB
    intrinsics to the depth grid, invert the pinhole projection, then convert
    image axes (right/down/forward) to ARKit camera axes (right/up/back).
    Integer pixel coordinates are used before resolution scaling.
    """
    if type(stride) is not int or stride < 1 or min_confidence not in (1, 2):
        raise ValueError('stride must be positive and confidence must be 1 or 2')
    if not (math.isfinite(near) and math.isfinite(far) and 0 < near < far):
        raise ValueError('depth range must be finite with 0 < near < far')
    if depth.ndim != 2 or confidence.shape != depth.shape:
        raise ValueError('depth and confidence grids must match')
    height, width = depth.shape
    rgb_width, rgb_height = rgb_size
    if rgb_width <= 0 or rgb_height <= 0:
        raise ValueError('invalid RGB dimensions')
    k = np.asarray(intrinsics, dtype=float)
    transform = np.asarray(pose, dtype=float)
    if k.shape != (3, 3) or transform.shape != (4, 4):
        raise ValueError('invalid camera matrices')
    depth_k = np.diag([width / rgb_width, height / rgb_height, 1]) @ k
    ys, xs = np.mgrid[0:height:stride, 0:width:stride]
    z = depth[ys, xs]
    levels = confidence[ys, xs]
    keep = np.isfinite(z) & (z >= near) & (z <= far) & (levels >= min_confidence) & (levels <= 2)
    xs, ys, z = xs[keep], ys[keep], z[keep]
    try:
        rays = np.linalg.solve(depth_k, np.stack([xs, ys, np.ones_like(xs)]))
    except np.linalg.LinAlgError as exc:
        raise ValueError('singular camera intrinsics') from exc
    if not np.allclose(rays[2], 1):
        raise ValueError('unsupported camera intrinsics')
    camera = (rays * z).T * [1, -1, -1]
    world = camera @ transform[:3, :3].T + transform[:3, 3]
    if not np.isfinite(world).all():
        raise ValueError('nonfinite reconstructed points')
    return world, np.stack([xs, ys], axis=1)


def reconstruct(root, output, *, stride=2, min_confidence=2, near=0.2, far=8.0):
    root, output = Path(root).resolve(), Path(output).resolve()
    if output == root or root in output.parents:
        raise ValueError('output must be outside the original capture directory')
    if output.exists():
        raise ValueError('output already exists; choose a new directory')
    checks = inspect_bundle(root)
    if not checks['capture_checks_passed']:
        raise ValueError('capture integrity/completeness checks failed; run validate_capture.py')
    # Spool vertices to disk: a long room scan must not accumulate all points in RAM.
    count = 0
    bounds_min = np.full(3, np.inf)
    bounds_max = np.full(3, -np.inf)
    trajectory = []
    with tempfile.TemporaryFile() as vertices:
        for row in checks['frames']:
            if not row['capture_eligible']:
                continue
            record = json.loads(local_file(root, row['file']).read_text())
            dw, dh = record['depth_resolution']
            depth = np.frombuffer(asset(root, record['depth']), dtype='<f4').reshape(dh, dw)
            confidence = np.frombuffer(asset(root, record['confidence']), dtype='u1').reshape(dh, dw)
            pose = matrix(record['camera_to_world'], 4)
            size = record['image_resolution']
            points, pixels = unproject(depth, confidence, matrix(record['intrinsics'], 3), pose, size,
                                       stride=stride, min_confidence=min_confidence, near=near, far=far)
            trajectory.append({'frame':row['file'], 'timestamp_s':row['timestamp_s'],
                               'camera_to_world':pose.ravel().tolist(), 'points':len(points)})
            if not len(points):
                continue
            with Image.open(io.BytesIO(asset(root, record['rgb']))) as image:
                rgb = np.asarray(image.convert('RGB'))
            u = np.minimum(np.rint(pixels[:, 0] * size[0] / dw).astype(int), size[0] - 1)
            v = np.minimum(np.rint(pixels[:, 1] * size[1] / dh).astype(int), size[1] - 1)
            packed = np.empty(len(points), dtype=VERTEX)
            packed['xyz'], packed['rgb'] = points, rgb[v, u]
            vertices.write(packed.tobytes())
            count += len(points)
            bounds_min = np.minimum(bounds_min, points.min(axis=0))
            bounds_max = np.maximum(bounds_max, points.max(axis=0))
        if not count:
            raise ValueError('no samples meet the selected confidence/depth range')
        centers = np.array([row['camera_to_world'] for row in trajectory]).reshape(-1, 4, 4)[:, :3, 3]
        report = {
            'format':'immersive-depth-diagnostic-v1', 'session_id':checks['session_id'],
            'calibration_validated':False, 'registration_performed':False,
            'units':'meters', 'axes':'ARKit gravity-aligned world; y_up',
            'pixel_sampling':'integer depth-grid coordinates; native RGB intrinsics scaled by resolution ratio',
            'points':count, 'frames_used':sum(row['points'] > 0 for row in trajectory),
            'frames_skipped_capture_checks':checks['frames_inspected'] - checks['eligible_observations'],
            'stride':stride, 'min_confidence':min_confidence, 'axial_depth_range_m':[near, far],
            'bounds_min_m':bounds_min.tolist(), 'bounds_max_m':bounds_max.tolist(),
            'camera_extent_m':np.ptp(centers, axis=0).tolist(),
            'camera_path_length_m':float(np.linalg.norm(np.diff(centers, axis=0), axis=1).sum()),
            'limitation':'Recorded ARKit poses only. No drift correction, room completeness, projector correspondences or alignment accuracy established.',
        }
        output.mkdir(parents=True, exist_ok=False)
        try:
            header = ('ply\nformat binary_little_endian 1.0\n'
                      'comment Diagnostic only; ARKit world coordinates in meters\n'
                      f'element vertex {count}\nproperty float x\nproperty float y\nproperty float z\n'
                      'property uchar red\nproperty uchar green\nproperty uchar blue\nend_header\n')
            with (output / 'scene.ply').open('wb') as cloud:
                cloud.write(header.encode('ascii'))
                vertices.seek(0)
                shutil.copyfileobj(vertices, cloud)
            (output / 'trajectory.json').write_text(json.dumps(trajectory, indent=2) + '\n')
            (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
        except BaseException:
            shutil.rmtree(output)
            raise
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--out', type=Path, required=True, help='New output directory outside the capture')
    parser.add_argument('--stride', type=int, default=2)
    parser.add_argument('--min-confidence', type=int, choices=(1, 2), default=2)
    parser.add_argument('--near', type=float, default=0.2)
    parser.add_argument('--far', type=float, default=8.0)
    args = parser.parse_args()
    try:
        report = reconstruct(args.directory, args.out, stride=args.stride,
                             min_confidence=args.min_confidence, near=args.near, far=args.far)
    except (ValueError, TypeError, KeyError, OSError) as exc:
        parser.exit(1, f'Reconstruction refused: {exc}\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
