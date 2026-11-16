#!/usr/bin/env python3
"""Extract observed vertical surface segments for offline phone-scan diagnosis.

No room shape, wall count, ceiling, floor or projector placement is assumed.
Even-indexed frames fit the draft; odd-indexed frames measure repeatability.
This split is not independent ground truth and cannot establish accuracy.
"""
import argparse
import json
import math
from pathlib import Path

import numpy as np

from reconstruct_capture import unproject
from validate_capture import asset, inspect_bundle, local_file, matrix


def extract_surfaces(points, *, voxel=.02, tolerance=.04, min_span=1.0,
                     min_support=60, max_surfaces=12, trials=1000):
    points = np.asarray(points,dtype=float)
    if points.ndim!=2 or points.shape[1]!=2 or not np.isfinite(points).all():
        raise ValueError('finite XZ points required')
    if any(not math.isfinite(v) or v<=0 for v in (voxel,tolerance,min_span)):
        raise ValueError('positive finite surface dimensions required')
    if any(type(v) is not int or v<1 for v in (min_support,max_surfaces,trials)) or min_support<3:
        raise ValueError('positive integer search limits and at least three support points required')
    if not len(points):return []
    # One vote per occupied voxel, so standing still cannot dominate a fit by
    # collecting many copies of the same visible patch.
    _,indices = np.unique(np.floor(points/voxel).astype(np.int64),axis=0,return_index=True)
    remaining = points[indices]
    random = np.random.default_rng(7361)
    surfaces = []
    for _ in range(max_surfaces):
        if len(remaining)<min_support:break
        best = None
        best_count = 0
        for _ in range(trials):
            a,b = remaining[random.choice(len(remaining),2,replace=False)]
            direction = b-a
            length = np.linalg.norm(direction)
            if length<min_span/2:continue
            tangent = direction/length
            normal = np.array([-tangent[1],tangent[0]])
            offset = normal@a
            mask = np.abs(remaining@normal-offset)<=tolerance
            count = int(mask.sum())
            if count<=best_count:continue
            projected = remaining[mask]@tangent
            if np.ptp(projected)<min_span:continue
            best,best_count = mask,count
        if best_count<min_support:break
        for _ in range(3):
            sample = remaining[best]
            center = sample.mean(axis=0)
            _,vectors = np.linalg.eigh((sample-center).T@(sample-center))
            normal = vectors[:,0]
            if normal[0]<0 or (normal[0]==0 and normal[1]<0):normal = -normal
            offset = float(normal@center)
            best = np.abs(remaining@normal-offset)<=tolerance
            if int(best.sum())<min_support:break
        if int(best.sum())<min_support:break
        tangent = np.array([-normal[1],normal[0]])
        along = remaining[best]@tangent
        limits = np.percentile(along,[1,99])
        if limits[1]-limits[0]<min_span:break
        bins = np.unique(np.floor((along[(along>=limits[0])&(along<=limits[1])]-limits[0])/voxel).astype(int))
        groups = np.split(bins,np.flatnonzero(np.diff(bins)>1)+1)
        intervals = [[float(limits[0]+g[0]*voxel),float(min(limits[1],limits[0]+(g[-1]+1)*voxel))]
                     for g in groups if len(g)]
        residual = np.abs(remaining[best]@normal-offset)
        surfaces.append({'id':len(surfaces),'normal_xz':normal.tolist(),'offset_m':offset,
                         'tangent_xz':tangent.tolist(),'along_limits_m':limits.tolist(),
                         'observed_segment_xz':[(normal*offset+tangent*v).tolist() for v in limits],
                         'observed_span_m':float(limits[1]-limits[0]),
                         'supported_along_intervals_m':intervals,
                         'along_coverage_fraction':sum(b-a for a,b in intervals)/float(limits[1]-limits[0]),
                         'occupied_voxel_support':int(best.sum()),
                         'training_p95_absolute_error_m':float(np.percentile(residual,95))})
        remaining = remaining[~best]
    return surfaces


def assess_frame(points, surfaces, *, association_limit=.3):
    points = np.asarray(points,dtype=float).reshape(-1,2)
    result = {'slice_points':len(points),'associated_points':0,'ambiguous_points':0,'surfaces':[]}
    if not len(points) or not surfaces:return result
    normals = np.array([s['normal_xz'] for s in surfaces])
    offsets = np.array([s['offset_m'] for s in surfaces])
    tangents = np.array([s['tangent_xz'] for s in surfaces])
    signed = points@normals.T-offsets
    along = points@tangents.T
    distance = np.abs(signed)
    for index,surface in enumerate(surfaces):
        supported = np.zeros(len(points),dtype=bool)
        for a,b in surface['supported_along_intervals_m']:
            supported |= (along[:,index]>=a-.02)&(along[:,index]<=b+.02)
        distance[~supported,index] = np.inf
    assignments = distance.argmin(axis=1)
    nearest = distance[np.arange(len(points)),assignments]
    if len(surfaces)>1:
        ranked = np.partition(distance,1,axis=1)[:,:2]
        supported = np.isfinite(ranked[:,0]) & np.isfinite(ranked[:,1]) & (ranked[:,0]<=association_limit)
        result['ambiguous_points'] = int(((ranked[supported,1]-ranked[supported,0])<.02).sum())
    for index,surface in enumerate(surfaces):
        mask = (assignments==index)&(nearest<=association_limit)
        if not mask.any():continue
        values = signed[mask,index]
        result['associated_points'] += len(values)
        result['surfaces'].append({'surface':surface['id'],'points':len(values),
                                  'median_signed_offset_m':float(np.median(values)),
                                  'p95_absolute_error_m':float(np.percentile(np.abs(values),95))})
    return result


def inspect_walls(root, *, y_min=-.5, y_max=.5, stride=4, tolerance=.04):
    if not math.isfinite(y_min) or not math.isfinite(y_max) or y_min>=y_max:
        raise ValueError('finite ordered camera-relative Y slice required')
    checks = inspect_bundle(root)
    if not checks['capture_checks_passed']:
        raise ValueError('capture integrity/completeness checks failed')
    root = Path(root)
    observations = []
    for row in checks['frames']:
        if not row['capture_eligible']:continue
        record = json.loads(local_file(root,row['file']).read_text())
        width,height = record['depth_resolution']
        depth = np.frombuffer(asset(root,record['depth']),dtype='<f4').reshape(height,width)
        confidence = np.frombuffer(asset(root,record['confidence']),dtype='u1').reshape(height,width)
        points,_ = unproject(depth,confidence,matrix(record['intrinsics'],3),matrix(record['camera_to_world'],4),
                             record['image_resolution'],stride=stride)
        mask = (points[:,1]>=y_min)&(points[:,1]<=y_max)
        observations.append((row,points[mask][:,[0,2]]))
    training = [points for row,points in observations if row['index']%2==0 and len(points)]
    if not training:raise ValueError('no training points in the selected Y slice')
    surfaces = extract_surfaces(np.concatenate(training),tolerance=tolerance)
    frames = []
    for row,points in observations:
        result = assess_frame(points,surfaces)
        result.update(frame=row['file'],index=row['index'],timestamp_s=row['timestamp_s'],
                      split='training' if row['index']%2==0 else 'held_out')
        frames.append(result)
    for surface in surfaces:
        held = [value for frame in frames if frame['split']=='held_out'
                for value in frame['surfaces'] if value['surface']==surface['id'] and value['points']>=20]
        surface['held_out_frames_with_20_points'] = len(held)
        surface['held_out_median_frame_offset_m'] = float(np.median([v['median_signed_offset_m'] for v in held])) if held else None
        surface['held_out_worst_frame_p95_error_m'] = max((v['p95_absolute_error_m'] for v in held),default=None)
    return {'format':'immersive-wall-draft-v1','session_id':checks['session_id'],
            'calibration_validated':False,'poses_modified':False,'room_configuration_changed':False,
            'y_slice_m':[y_min,y_max],'training_tolerance_m':tolerance,'association_limit_m':.3,
            'surfaces':surfaces,'frames':frames,
            'limitation':'Observed vertical surface segments only. No forced wall count, corner closure, ceiling/floor or projection boundaries. Interleaved frames share tracking errors; repeatability is not accuracy. Surface associations are nearest hypotheses, not established correspondences: candidates within 2 cm distance of one another are counted as ambiguous. Associations are gated at 30 cm and unmatched points remain unassigned.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory',type=Path)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--y-min',type=float,default=-.5)
    parser.add_argument('--y-max',type=float,default=.5)
    parser.add_argument('--stride',type=int,default=4)
    parser.add_argument('--tolerance',type=float,default=.04)
    args = parser.parse_args()
    try:
        if args.out.resolve()==args.directory.resolve() or args.directory.resolve() in args.out.resolve().parents:
            raise ValueError('output must be outside the original capture')
        result = inspect_walls(args.directory,y_min=args.y_min,y_max=args.y_max,stride=args.stride,tolerance=args.tolerance)
        with args.out.open('x') as stream:stream.write(json.dumps(result,indent=2)+'\n')
    except (ValueError,TypeError,KeyError,OSError) as exc:
        parser.exit(1,f'Wall draft refused: {exc}\n')
    print(f'Found {len(result["surfaces"])} observed surface segments; not a projector calibration')


if __name__=='__main__':
    main()
