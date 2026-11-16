#!/usr/bin/env python3
"""Compare recorded depth across phone views without fitting or modifying poses.

This is a coarse self-consistency diagnostic, not an independent accuracy test.
ARKit pose and depth errors may be correlated. Requires NumPy and Pillow.
"""
import argparse
import json
import math
from pathlib import Path

import numpy as np

from reconstruct_capture import unproject
from validate_capture import asset, inspect_bundle, local_file, matrix


def compare_direction(source, target, *, stride=4, tolerance=0.08):
    if not math.isfinite(tolerance) or tolerance <= 0:
        raise ValueError('depth tolerance must be positive and finite')
    points, _ = unproject(source['depth'], source['confidence'], source['k'],
                          source['pose'], source['rgb_size'], stride=stride)
    # Inverse of a rigid camera-to-world transform, then image camera axes.
    camera = (points - target['pose'][:3, 3]) @ target['pose'][:3, :3]
    camera *= [1, -1, -1]
    height, width = target['depth'].shape
    rgb_width, rgb_height = target['rgb_size']
    k = np.diag([width / rgb_width, height / rgb_height, 1]) @ target['k']
    camera = camera[np.isfinite(camera).all(axis=1) & (camera[:,2] > 0.2) & (camera[:,2] <= 8)]
    projected = camera @ k.T
    pixels = projected[:,:2] / projected[:,2:3]
    # Keep a full neighborhood inside the target, so corners, holes and depth
    # discontinuities aren't silently interpolated into matching surfaces.
    inside = (pixels[:,0] >= 1) & (pixels[:,0] < width-2) & (pixels[:,1] >= 1) & (pixels[:,1] < height-2)
    camera, pixels = camera[inside], pixels[inside]
    u, v = np.rint(pixels).astype(int).T
    neighborhoods = np.stack([target['depth'][v+dy, u+dx] for dy in (-1,0,1) for dx in (-1,0,1)], axis=1)
    confidence = np.stack([target['confidence'][v+dy, u+dx] for dy in (-1,0,1) for dx in (-1,0,1)], axis=1)
    valid = (np.isfinite(neighborhoods).all(axis=1) & (neighborhoods >= .2).all(axis=1) &
             (neighborhoods <= 8).all(axis=1) & (confidence == 2).all(axis=1))
    # Reject edges conservatively. This may reduce overlap on oblique surfaces;
    # missing evidence must remain missing rather than count as agreement.
    spread = np.ptp(neighborhoods, axis=1)
    valid &= spread <= tolerance * 2
    observed = target['depth'][v[valid], u[valid]]
    residual = camera[valid,2] - observed
    occluded = residual > tolerance
    visible = residual[~occluded]
    absolute = np.abs(visible)
    return {
        'source_points':len(points), 'target_interior_points':len(pixels),
        'target_rejected_edge_or_confidence':int((~valid).sum()),
        'occluded_points':int(occluded.sum()), 'compared_points':len(visible),
        'agreement_fraction':float((absolute <= tolerance).mean()) if len(visible) else None,
        'free_space_contradictions':int((visible < -tolerance).sum()),
        'median_absolute_depth_error_m':float(np.median(absolute)) if len(visible) else None,
        'p95_absolute_depth_error_m':float(np.percentile(absolute,95)) if len(visible) else None,
    }


def compare_pair(a, b, *, stride=4, tolerance=.08, min_points=100, min_baseline=.1):
    if type(min_points) is not int or min_points < 1 or not math.isfinite(min_baseline) or min_baseline <= 0:
        raise ValueError('positive minimum sample count and camera baseline required')
    baseline = float(np.linalg.norm(a['pose'][:3,3] - b['pose'][:3,3]))
    result = {'source':a['name'], 'target':b['name'], 'camera_baseline_m':baseline}
    if baseline < min_baseline:
        return dict(result, outcome='insufficient_translation')
    forward = compare_direction(a,b,stride=stride,tolerance=tolerance)
    backward = compare_direction(b,a,stride=stride,tolerance=tolerance)
    supported = [d for d in (forward,backward) if d['compared_points'] >= min_points]
    if any(d['agreement_fraction'] < .8 for d in supported):
        outcome = 'needs_review'
    elif len(supported) == 2:
        outcome = 'consistent_coarse_depth'
    else:
        outcome = 'insufficient_overlap'
    return dict(result, outcome=outcome, forward=forward, backward=backward)


def inspect_agreement(root, *, max_frames=64, stride=4, tolerance=.08, min_points=100, min_baseline=.1):
    if type(max_frames) is not int or not 2 <= max_frames <= 256:
        raise ValueError('max_frames must be between 2 and 256')
    if type(stride) is not int or stride < 1 or not math.isfinite(tolerance) or tolerance <= 0:
        raise ValueError('positive stride and finite depth tolerance required')
    root = Path(root)
    checks = inspect_bundle(root)
    if not checks['capture_checks_passed']:
        raise ValueError('capture integrity/completeness checks failed; run validate_capture.py')
    eligible = [row for row in checks['frames'] if row['capture_eligible']]
    indices = np.unique(np.linspace(0,len(eligible)-1,min(max_frames,len(eligible)),dtype=int))
    views = []
    for index in indices:
        row = eligible[index]
        record = json.loads(local_file(root,row['file']).read_text())
        width,height = record['depth_resolution']
        views.append({'name':row['file'], 'pose':matrix(record['camera_to_world'],4),
                      'k':matrix(record['intrinsics'],3), 'rgb_size':record['image_resolution'],
                      'depth':np.frombuffer(asset(root,record['depth']),dtype='<f4').reshape(height,width),
                      'confidence':np.frombuffer(asset(root,record['confidence']),dtype='u1').reshape(height,width)})
    pairs = {(i,i+lag) for lag in (1,2,4,8) for i in range(len(views)-lag)}
    if len(views)>1:
        pairs.add((0,len(views)-1))  # Also expose gross first/last loop disagreement.
    results = [compare_pair(views[i],views[j],stride=stride,tolerance=tolerance,
                            min_points=min_points,min_baseline=min_baseline) for i,j in sorted(pairs)]
    outcomes = ('consistent_coarse_depth','needs_review','insufficient_translation','insufficient_overlap')
    counts = {key:sum(p['outcome']==key for p in results) for key in outcomes}
    return {'format':'immersive-depth-agreement-v1', 'session_id':checks['session_id'],
            'calibration_validated':False, 'poses_modified':False,
            'frames_sampled':len(views), 'eligible_frames_available':len(eligible),
            'thresholds':{'depth_tolerance_m':tolerance,'min_points_per_direction':min_points,
                          'min_camera_baseline_m':min_baseline,'stride':stride,'min_agreement_fraction':.8},
            'pair_counts':counts, 'pairs':results,
            'limitation':'Coarse recorded pose/depth self-consistency only. Occluded points and depth edges are excluded. Shared drift, calibration accuracy, full-room coverage and projector correspondences remain unverified.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory',type=Path)
    parser.add_argument('--out',type=Path)
    parser.add_argument('--max-frames',type=int,default=64)
    parser.add_argument('--stride',type=int,default=4)
    parser.add_argument('--tolerance',type=float,default=.08,help='Coarse axial-depth tolerance in meters')
    parser.add_argument('--min-points',type=int,default=100)
    parser.add_argument('--min-baseline',type=float,default=.1)
    args = parser.parse_args()
    try:
        if args.out and (args.out.resolve()==args.directory.resolve() or args.directory.resolve() in args.out.resolve().parents):
            raise ValueError('report must be outside the original capture directory')
        report = inspect_agreement(args.directory,max_frames=args.max_frames,stride=args.stride,
                                   tolerance=args.tolerance,min_points=args.min_points,min_baseline=args.min_baseline)
        encoded = json.dumps(report,indent=2)+'\n'
        if args.out:
            with args.out.open('x') as output:output.write(encoded)
        else:print(encoded,end='')
    except (ValueError, TypeError, KeyError, OSError) as exc:
        parser.exit(1,f'Depth comparison refused: {exc}\n')


if __name__=='__main__':
    main()
